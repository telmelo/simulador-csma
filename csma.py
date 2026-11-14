#!/usr/bin/env python3
"""Simulador CSMA/CA (IEEE 802.11) para demonstração ao vivo no terminal.

Seminário "Redes sem fio e redes móveis" (Kurose & Ross, Seções 7.1–7.3).

Exemplos:
    python csma.py                                   # menu interativo
    python csma.py --cenario normal --semente 3      # animação slot a slot
    python csma.py --cenario normal --semente 3 --passo
    python csma.py --cenario sem-recuo
    python csma.py --cenario oculto
    python csma.py --cenario oculto --rts
    python csma.py --comparar
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from dataclasses import dataclass, field, replace

try:
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover
    sys.exit("Este programa precisa da biblioteca 'rich'. Instale com:  pip install rich")


# ════════════════════════════════════════════════════════════════════════
# Configuração e cenários
# ════════════════════════════════════════════════════════════════════════

@dataclass
class Config:
    """Parâmetros do protocolo (em slots) e da apresentação."""
    sifs: int = 1
    difs: int = 2
    dados: int | None = None      # None → usa o padrão do cenário (4 ou 6)
    ack: int = 1
    rts: int = 1
    cts: int = 1
    cw_min: int = 3               # faixa inicial de recuo: 0–3
    cw_max: int = 31              # faixa máxima de recuo: 0–31
    max_retx: int = 5             # máximo de retransmissões antes de descartar
    velocidade: float = 0.3       # segundos por slot na animação


@dataclass
class Cenario:
    chave: str
    nome: str                                  # nome exibido no cabeçalho/resumo
    estacoes: list[tuple[str, int, int]]       # (nome, chegada, nº de quadros)
    dados: int                                 # duração padrão dos DADOS
    semente: int
    recuo: bool = True
    rts: bool = False
    ocultos: frozenset = frozenset()           # pares de estações que não se ouvem


def _par(a: str, b: str) -> frozenset:
    return frozenset((a, b))


CENARIOS = {
    "normal": Cenario("normal", "normal",
                      [("A", 0, 2), ("B", 2, 1), ("C", 3, 1)], dados=4, semente=3),
    "sem-recuo": Cenario("sem-recuo", "sem recuo",
                         [("A", 0, 2), ("B", 2, 1), ("C", 3, 1)], dados=4, semente=3,
                         recuo=False),
    "oculto": Cenario("oculto", "oculto",
                      [("A", 0, 1), ("C", 3, 1)], dados=6, semente=6,
                      ocultos=frozenset({_par("A", "C")})),
}


def montar_cenario(chave: str, rts: bool = False, semente: int | None = None,
                   quadros: int | None = None, ocultos: frozenset | None = None) -> Cenario:
    base = CENARIOS[chave]
    c = replace(base, rts=rts or base.rts)
    if semente is not None:
        c.semente = semente
    if quadros is not None:
        c.estacoes = [(n, cheg, quadros) for n, cheg, _ in base.estacoes]
    if ocultos is not None:
        c.ocultos = ocultos
    if c.rts:
        c.nome = f"{base.nome} com RTS/CTS"
    elif c.ocultos and chave == "oculto":
        c.nome = f"{base.nome} sem RTS/CTS"
    return c


# ════════════════════════════════════════════════════════════════════════
# Simulação
# ════════════════════════════════════════════════════════════════════════

DADOS, RTS, ACK, CTS = "DADOS", "RTS", "ACK", "CTS"
AP = "AP"

# Estados da estação
OCIOSA, DIFS_, RECUO, PRONTA = "ociosa", "difs", "recuo", "pronta"
TRANSMITINDO, ESPERA_CTS, ESPERA_SIFS, ESPERA_ACK, FIM = (
    "transmitindo", "espera_cts", "espera_sifs", "espera_ack", "fim")


@dataclass(eq=False)
class Tx:
    """Um quadro no ar, ocupando os slots [inicio, fim]."""
    dono: str
    tipo: str
    inicio: int
    fim: int
    alvo: str | None = None
    colidiu_em: int | None = None   # slot em que a sobreposição foi detectada

    def ativa(self, t: int) -> bool:
        return self.inicio <= t <= self.fim

    @property
    def duracao(self) -> int:
        return self.fim - self.inicio + 1


@dataclass(eq=False)
class Estacao:
    nome: str
    chegada: int
    quadros: int
    cw: int
    estado: str = OCIOSA
    fila: int = 0
    cont_difs: int = 0
    recuo: int | None = None       # None = ainda não sorteou
    tentativas: int = 0            # retransmissões do quadro atual
    tx_em: int | None = None       # slot agendado para começar a transmitir
    tx: Tx | None = None
    prazo: int = -1                # slot em que termina a espera por ACK/CTS
    recebeu: bool = False
    nav_ate: int = -1
    entregues: int = 0
    descartados: int = 0
    colisoes: int = 0
    maior_cw: int = 0


@dataclass
class Evento:
    t: int
    quem: str
    msg: str
    cat: str


class Simulacao:
    LIMITE = 200_000

    def __init__(self, cen: Cenario, cfg: Config):
        self.cen = cen
        self.cfg = cfg
        self.dados = cfg.dados if cfg.dados is not None else cen.dados
        self.recuo = cen.recuo
        self.rts = cen.rts
        self.rng = random.Random(cen.semente)
        self.est = [Estacao(n, cheg, q, cw=cfg.cw_min, maior_cw=cfg.cw_min,
                            estado=OCIOSA if q > 0 else FIM)
                    for n, cheg, q in cen.estacoes]
        self.por_nome = {e.nome: e for e in self.est}
        self.txs: list[Tx] = []
        self.ativas: list[Tx] = []
        self.agenda_ap: list[tuple[int, str, str]] = []   # (inicio, tipo, alvo)
        self.colunas: list[dict] = []
        self.eventos: list[Evento] = []
        self.eventos_por_slot: dict[int, list[Evento]] = {}
        self.colisoes = 0          # eventos de colisão no AP
        self.slots_uteis = 0       # slots de DADOS entregues com sucesso
        self.total = 0

    # ── utilidades ─────────────────────────────────────────────────────
    def ouve(self, a: str, b: str) -> bool:
        return _par(a, b) not in self.cen.ocultos

    def ocupado(self, e: Estacao, t: int) -> bool:
        if e.nav_ate >= t:
            return True
        return any(x.ativa(t) and x.dono != e.nome and (x.dono == AP or self.ouve(e.nome, x.dono))
                   for x in self.ativas)

    def ocultos_transmitindo(self, e: Estacao, t: int) -> list[str]:
        """Estações que estão transmitindo, mas que `e` não ouve."""
        return sorted({x.dono for x in self.ativas
                       if x.ativa(t) and x.inicio < t and x.dono not in (AP, e.nome)
                       and not self.ouve(e.nome, x.dono)})

    @staticmethod
    def nota_oculta(nomes: list[str]) -> str:
        if not nomes:
            return ""
        verbo = "que está transmitindo" if len(nomes) == 1 else "que estão transmitindo"
        return f" (não ouve {', '.join(nomes)}, {verbo}!)"

    def log(self, t: int, quem: str, msg: str, cat: str) -> None:
        ev = Evento(t, quem, msg, cat)
        self.eventos.append(ev)
        self.eventos_por_slot.setdefault(t, []).append(ev)

    def sorteia(self, e: Estacao) -> int:
        return self.rng.randint(0, e.cw)

    def faixa(self, cw: int) -> str:
        return f"0–{cw}"

    # ── laço principal ─────────────────────────────────────────────────
    def executar(self) -> "Simulacao":
        t = 0
        while True:
            self.passo(t)
            if all(e.estado == FIM for e in self.est) and not self.agenda_ap \
                    and not any(x.fim > t for x in self.ativas):
                break
            t += 1
            if t >= self.LIMITE:
                break
        self.total = t + 1
        return self

    def passo(self, t: int) -> None:
        cfg = self.cfg
        col: dict = {}
        self.ativas = [x for x in self.ativas if x.fim >= t]

        # (1) Inicia as transmissões agendadas para este slot
        for e in self.est:
            if e.estado == PRONTA and e.tx_em == t:
                self._inicia(e, RTS if self.rts else DADOS, t)
            elif e.estado == ESPERA_SIFS and e.tx_em == t:
                self._inicia(e, DADOS, t)
        for ag in [a for a in self.agenda_ap if a[0] == t]:
            self.agenda_ap.remove(ag)
            _, tipo, alvo = ag
            dur = cfg.ack if tipo == ACK else cfg.cts
            x = Tx(AP, tipo, t, t + dur - 1, alvo=alvo)
            self.txs.append(x)
            self.ativas.append(x)
            if tipo == CTS:
                nav_fim = x.fim + cfg.sifs + self.dados + cfg.sifs + cfg.ack
                self.log(t, AP, f"envia CTS para {alvo} → canal reservado até t={nav_fim}", "cts")
                for e in self.est:
                    if e.nome == alvo:
                        continue
                    e.nav_ate = max(e.nav_ate, nav_fim)
                    if e.estado not in (OCIOSA, FIM):
                        self.log(t, e.nome,
                                 f"ouviu o CTS → silêncio por reserva (NAV) até t={nav_fim}", "nav")
        self._verifica_colisoes(t)

        # (2) Chegadas de quadros
        for e in self.est:
            if e.chegada == t and e.quadros > 0:
                e.fila += e.quadros
                if e.estado in (OCIOSA, FIM):
                    if not self.ocupado(e, t):
                        e.recuo = None
                        nota = self.nota_oculta(self.ocultos_transmitindo(e, t))
                        self.log(t, e.nome, f"tem quadro; canal livre → espera DIFS{nota}", "chegada")
                    elif self.recuo:
                        e.recuo = self.sorteia(e)
                        self.log(t, e.nome,
                                 f"tem quadro; canal OCUPADO → sorteia recuo = {e.recuo} "
                                 f"(faixa {self.faixa(e.cw)})", "sorteio")
                    else:
                        e.recuo = None
                        self.log(t, e.nome, "tem quadro; canal OCUPADO → espera o DIFS (sem recuo)",
                                 "chegada")
                    e.estado = DIFS_
                    e.cont_difs = 0

        # (3) Sensoriamento do canal e atualização de DIFS / recuo
        for e in self.est:
            nav = e.nav_ate >= t
            if e.estado == DIFS_:
                if self.ocupado(e, t):
                    e.cont_difs = 0
                    if e.recuo is None and self.recuo:
                        e.recuo = self.sorteia(e)
                        self.log(t, e.nome,
                                 f"canal ocupou durante o DIFS → sorteia recuo = {e.recuo} "
                                 f"(faixa {self.faixa(e.cw)})", "sorteio")
                    col[e.nome] = ("nav",) if nav else ("ocup",)
                else:
                    e.cont_difs += 1
                    col[e.nome] = ("difs",)
                    if e.cont_difs >= cfg.difs:
                        if e.recuo:
                            e.estado = RECUO
                        else:
                            e.estado, e.tx_em = PRONTA, t + 1
            elif e.estado == RECUO:
                if self.ocupado(e, t):
                    e.estado, e.cont_difs = DIFS_, 0
                    self.log(t, e.nome, f"canal ocupado → CONGELA contador em {e.recuo}", "congela")
                    col[e.nome] = ("nav",) if nav else ("ocup",)
                else:
                    col[e.nome] = ("cont", e.recuo)
                    e.recuo -= 1
                    if e.recuo == 0:
                        e.estado, e.tx_em = PRONTA, t + 1
            elif e.estado == TRANSMITINDO:
                col[e.nome] = ("tx", e.tx)
            elif e.estado in (ESPERA_ACK, ESPERA_CTS, ESPERA_SIFS):
                col[e.nome] = ("espera",)
            else:
                col[e.nome] = ("nav",) if nav else ("nada",)

        ap_ativas = [x for x in self.ativas if x.dono == AP and x.ativa(t)]
        col[AP] = ("tx", ap_ativas[0]) if ap_ativas else ("nada",)
        col["Canal"] = ("canal", [x for x in self.ativas if x.ativa(t)])

        # (4) Fim de transmissões: o AP decide entre ACK/CTS e colisão
        for x in [x for x in self.ativas if x.fim == t]:
            if x.dono == AP:
                self.por_nome[x.alvo].recebeu = True
                continue
            e = self.por_nome[x.dono]
            e.recebeu = False
            if x.tipo == DADOS:
                if x.colidiu_em is None:
                    self.agenda_ap.append((t + cfg.sifs + 1, ACK, e.nome))
                e.estado, e.prazo = ESPERA_ACK, t + cfg.sifs + cfg.ack
            else:
                if x.colidiu_em is None:
                    self.agenda_ap.append((t + cfg.sifs + 1, CTS, e.nome))
                e.estado, e.prazo = ESPERA_CTS, t + cfg.sifs + cfg.cts

        # (5) Sucessos e timeouts
        for e in self.est:
            if e.prazo != t:
                continue
            if e.estado == ESPERA_ACK:
                if e.recebeu:
                    e.entregues += 1
                    e.fila -= 1
                    self.slots_uteis += e.tx.duracao
                    e.tentativas, e.cw = 0, cfg.cw_min
                    self._proximo(e, t, "recebe ACK ✔", "ack")
                else:
                    self._falha(e, t, "sem ACK")
            elif e.estado == ESPERA_CTS:
                if e.recebeu:
                    e.estado, e.tx_em = ESPERA_SIFS, t + cfg.sifs + 1
                    self.log(t, e.nome, "recebe CTS ✔ → canal reservado; envia DADOS após o SIFS", "cts")
                else:
                    self._falha(e, t, "sem CTS")

        self.colunas.append(col)

    def _inicia(self, e: Estacao, tipo: str, t: int) -> None:
        dur = self.dados if tipo == DADOS else self.cfg.rts
        nota = self.nota_oculta(self.ocultos_transmitindo(e, t))
        x = Tx(e.nome, tipo, t, t + dur - 1, alvo=AP)
        self.txs.append(x)
        self.ativas.append(x)
        e.tx, e.estado, e.tx_em, e.recuo = x, TRANSMITINDO, None, None
        if tipo == DADOS:
            self.log(t, e.nome, f"transmite DADOS{nota}", "tx")
        else:
            self.log(t, e.nome, f"transmite RTS (pede reserva do canal){nota}", "rts")

    def _verifica_colisoes(self, t: int) -> None:
        """Colisão = sobreposição, no AP, de quadros vindos das estações.

        Um quadro que chega enquanto o próprio AP transmite também se perde
        (o AP não recebe e transmite ao mesmo tempo)."""
        est = [x for x in self.ativas if x.dono != AP and x.ativa(t)]
        ap = [x for x in self.ativas if x.dono == AP and x.ativa(t)]
        perdidas = est if (len(est) >= 2 or (est and ap)) else []
        novas = [x for x in perdidas if x.colidiu_em is None]
        if novas:
            self.colisoes += 1
            for x in novas:
                x.colidiu_em = t

    def _proximo(self, e: Estacao, t: int, prefixo: str, cat: str) -> None:
        """Depois de um sucesso ou descarte: passa ao próximo quadro, se houver."""
        if e.fila > 0:
            e.estado, e.cont_difs = DIFS_, 0
            if self.recuo:
                e.recuo = self.sorteia(e)
                self.log(t, e.nome, f"{prefixo} → próximo quadro, sorteia recuo = {e.recuo}", cat)
            else:
                e.recuo = None
                self.log(t, e.nome, f"{prefixo} → próximo quadro, espera o DIFS", cat)
        else:
            e.estado = FIM
            self.log(t, e.nome, f"{prefixo} → terminou", cat)

    def _falha(self, e: Estacao, t: int, motivo: str) -> None:
        cfg = self.cfg
        perdida = e.tx is not None and e.tx.colidiu_em is not None
        if perdida:
            e.colisoes += 1
        causa = "colisão no AP" if perdida else "quadro perdido"
        e.tentativas += 1
        if e.tentativas > cfg.max_retx:
            e.descartados += 1
            e.fila -= 1
            e.tentativas, e.cw = 0, cfg.cw_min
            self._proximo(e, t, f"{motivo} ✘ ({causa}) → DESISTE após {cfg.max_retx} "
                                f"retransmissões: quadro DESCARTADO", "descarte")
            return
        e.estado, e.cont_difs = DIFS_, 0
        if self.recuo:
            antes = e.cw
            e.cw = min(2 * e.cw + 1, cfg.cw_max)
            e.maior_cw = max(e.maior_cw, e.cw)
            e.recuo = self.sorteia(e)
            if e.cw != antes:
                acao = f"faixa dobra p/ {self.faixa(e.cw)}"
            else:
                acao = f"faixa já no máximo ({self.faixa(e.cw)})"
            self.log(t, e.nome, f"{motivo} ✘ ({causa}) → {acao}, sorteia recuo = {e.recuo}", "falha")
        else:
            e.recuo = None
            self.log(t, e.nome, f"{motivo} ✘ ({causa}) → retransmite logo após o DIFS "
                                f"(retransmissão {e.tentativas} de {cfg.max_retx})", "falha")

    # ── estatísticas ───────────────────────────────────────────────────
    @property
    def entregues(self) -> int:
        return sum(e.entregues for e in self.est)

    @property
    def quadros(self) -> int:
        return sum(e.quadros for e in self.est)

    @property
    def descartados(self) -> int:
        return sum(e.descartados for e in self.est)

    @property
    def uso(self) -> float:
        return self.slots_uteis / self.total if self.total else 0.0


# ════════════════════════════════════════════════════════════════════════
# Apresentação no terminal
# ════════════════════════════════════════════════════════════════════════

ESTILO = {
    "difs": "yellow",
    "cont": "bold bright_yellow",
    "ocup": "grey50",
    "dados": "green",
    "colisao": "bold red",
    "ack": "bold bright_blue",
    "rtscts": "bold cyan",
    "nav": "magenta",
    "nada": "",
}

ESTILO_EVENTO = {
    "chegada": "",
    "sorteio": "bright_yellow",
    "congela": "grey62",
    "tx": "green",
    "rts": "cyan",
    "cts": "cyan",
    "nav": "magenta",
    "ack": "bold green",
    "falha": "bold red",
    "descarte": "bold red",
}

ROTULO = 7   # largura da coluna de rótulos (" Canal ")


class Tela:
    def __init__(self, sim: Simulacao, console: Console):
        self.sim = sim
        self.con = console

    # ── Tela 1: cabeçalho ──────────────────────────────────────────────
    def cabecalho(self) -> Panel:
        s, c, cfg = self.sim, self.sim.cen, self.sim.cfg
        nomes = ", ".join(e.nome for e in s.est)
        linhas = [
            f"Cenário: {c.nome:<22} Estações: {nomes}  →  AP      Semente: {c.semente}",
            f"Tempos (slots): DIFS = {cfg.difs}   SIFS = {cfg.sifs}   DADOS = {s.dados}   ACK = {cfg.ack}"
            + (f"   RTS = {cfg.rts}   CTS = {cfg.cts}" if s.rts else ""),
        ]
        if s.recuo:
            linhas.append(f"Recuo: faixa inicial 0–{cfg.cw_min}, dobra a cada falha (máx. 0–{cfg.cw_max})")
        else:
            linhas.append("Recuo: DESATIVADO — cada estação transmite logo após o DIFS")
        if c.ocultos:
            pares = ", ".join(" e ".join(sorted(p)) for p in sorted(c.ocultos, key=sorted))
            linhas.append(f"Audição: todos ouvem o AP; {pares} NÃO se ouvem (terminal oculto)")
        if s.rts:
            linhas.append("RTS/CTS: ativado — o CTS do AP reserva o canal (NAV) para dados + ACK")
        return Panel("\n".join(linhas), title="Simulador CSMA/CA (802.11)", box=box.ROUNDED,
                     expand=False, padding=(0, 1))

    # ── Tela 2: linha do tempo ─────────────────────────────────────────
    def _celula_tx(self, x: Tx, T: int, vivo: bool) -> tuple[str, str]:
        if x.dono == AP:
            return ("AK", ESTILO["ack"]) if x.tipo == ACK else ("CT", ESTILO["rtscts"])
        colidiu = x.colidiu_em is not None and x.colidiu_em <= T
        if colidiu:
            estilo = ESTILO["colisao"]
            if vivo and T <= x.fim + self.sim.cfg.sifs + self.sim.cfg.ack:
                estilo += " blink"
            return ("XX" if x.tipo == DADOS else "RT"), estilo
        return ("██", ESTILO["dados"]) if x.tipo == DADOS else ("RT", ESTILO["rtscts"])

    def _celula(self, c: tuple, T: int, vivo: bool) -> tuple[str, str]:
        tipo = c[0]
        if tipo == "difs":
            return "d ", ESTILO["difs"]
        if tipo == "cont":
            return f"{c[1]:<2}", ESTILO["cont"]
        if tipo == "ocup":
            return "- ", ESTILO["ocup"]
        if tipo == "nav":
            return "░░", ESTILO["nav"]
        if tipo == "tx":
            return self._celula_tx(c[1], T, vivo)
        if tipo == "canal":
            txs = c[1]
            est = [x for x in txs if x.dono != AP]
            if len(est) >= 2:
                estilo = ESTILO["colisao"] + (" blink" if vivo else "")
                return ("RT" if all(x.tipo == RTS for x in est) else "XX"), estilo
            if est:
                return self._celula_tx(est[0], T, vivo)
            if txs:
                return self._celula_tx(txs[0], T, vivo)
            return "  ", ""
        return "· ", ESTILO["nada"]

    def slots_por_bloco(self) -> int:
        return max(10, (self.con.width - ROTULO - 1) // 2)

    def grade(self, T: int, vivo: bool = False) -> list[Text]:
        """Linhas da linha do tempo com os slots 0..T, divididas em blocos."""
        s = self.sim
        n = self.slots_por_bloco()
        linhas_nomes = [e.nome for e in s.est] + [AP, "Canal"]
        saida: list[Text] = []
        for b in range(0, T + 1, n):
            fim = min(b + n, T + 1)
            if b:
                saida.append(Text(""))
            regua = [" "] * (2 * (fim - b))
            for slot in range(b, fim):
                if slot % 5 == 0:
                    rot = str(slot)
                    pos = 2 * (slot - b)
                    for i, ch in enumerate(rot):
                        if pos + i < len(regua):
                            regua[pos + i] = ch
            saida.append(Text(f" {'t':<{ROTULO - 1}}" + "".join(regua), style="dim"))
            for nome in linhas_nomes:
                linha = Text(f" {nome:<{ROTULO - 1}}", style="bold")
                for slot in range(b, fim):
                    txt, est = self._celula(s.colunas[slot][nome], T, vivo)
                    linha.append(txt, style=est or None)
                saida.append(linha)
        return saida

    def legenda(self) -> list[Text]:
        grupos = [
            [("d", ESTILO["difs"], "esperando DIFS"),
             ("3 2 1", ESTILO["cont"], "recuo descendo"),
             ("-", ESTILO["ocup"], "canal ocupado / contador congelado"),
             ("·", "", "nada / aguardando ACK")],
            [("██", ESTILO["dados"], "DADOS"),
             ("XX", ESTILO["colisao"], "colisão"),
             ("AK", ESTILO["ack"], "ACK"),
             ("RT/CT", ESTILO["rtscts"], "RTS/CTS"),
             ("░░", ESTILO["nav"], "silêncio por reserva (NAV)")],
        ]
        linhas = []
        for n, grupo in enumerate(grupos):
            t = Text(" Legenda: " if n == 0 else " " * 10, style="bold")
            for i, (simb, est, desc) in enumerate(grupo):
                if i:
                    t.append("   ")
                t.append(simb, style=est or None)
                t.append(f" {desc}", style="dim")
            linhas.append(t)
        return linhas

    # ── Tela 3: registro de eventos ────────────────────────────────────
    @staticmethod
    def linha_evento(ev: Evento) -> Text:
        t = Text(f" t={ev.t:<5}", style="dim")
        t.append(f"{ev.quem:<5}", style="bold")
        t.append(ev.msg, style=ESTILO_EVENTO.get(ev.cat) or None)
        return t

    def eventos(self, T: int, maximo: int | None = None) -> list[Text]:
        evs = [ev for ev in self.sim.eventos if ev.t <= T]
        if maximo is not None and len(evs) > maximo:
            evs = evs[-maximo:]
        return [self.linha_evento(ev) for ev in evs]

    # ── Tela 4: resumo ─────────────────────────────────────────────────
    def resumo(self) -> Group:
        s = self.sim
        com_descarte = s.descartados > 0
        tab = Table(title=f"Resumo — cenário {s.cen.nome}", box=box.HEAVY_HEAD, title_style="bold")
        tab.add_column("Estação", justify="center")
        tab.add_column("Quadros entregues")
        tab.add_column("Colisões")
        if com_descarte:
            tab.add_column("Descartados")
        tab.add_column("Maior faixa de recuo")
        for e in s.est:
            linha = [e.nome, f"{e.entregues} / {e.quadros}", str(e.colisoes)]
            if com_descarte:
                linha.append(str(e.descartados))
            linha.append(f"0–{e.maior_cw}" if s.recuo else "— (sem recuo)")
            tab.add_row(*linha)
        rodape = Text(f" Tempo total: {s.total} slots    Uso do canal com dados úteis: "
                      f"{s.slots_uteis}/{s.total} = {round(100 * s.uso)}%")
        return Group(tab, rodape)

    # ── composição ─────────────────────────────────────────────────────
    def quadro_ao_vivo(self, T: int, dica: str | None) -> Group:
        grade = self.grade(T, vivo=True)
        usadas = len(grade) + 6 + (1 if dica else 0)
        cabem = max(4, self.con.height - usadas - 8)   # 8 ≈ cabeçalho impresso acima
        partes: list = [*grade, Text(""), *self.legenda(), Text(""),
                        Text(" Registro de eventos", style="bold underline"),
                        *self.eventos(T, cabem)]
        if dica:
            partes += [Text(""), Text(dica, style="reverse")]
        return Group(*partes)

    def imprimir_final(self) -> None:
        T = self.sim.total - 1
        self.con.print(Text(" Linha do tempo", style="bold underline"))
        for linha in self.grade(T):
            self.con.print(linha, no_wrap=True, overflow="ignore")
        self.con.print()
        for linha in self.legenda():
            self.con.print(linha)
        self.con.print()
        self.con.print(Text(" Registro de eventos", style="bold underline"))
        for linha in self.eventos(T):
            self.con.print(linha)
        self.con.print()
        self.con.print(self.resumo())


def esperar_enter() -> None:
    """Espera o apresentador apertar Enter, sem ecoar nada (não bagunça a animação)."""
    if os.name == "nt":
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                return
            if ch == "\x03":
                raise KeyboardInterrupt
    elif sys.stdin.isatty():
        import termios
        fd = sys.stdin.fileno()
        antigo = termios.tcgetattr(fd)
        novo = termios.tcgetattr(fd)
        novo[3] &= ~(termios.ECHO | termios.ICANON)
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, novo)
            while True:
                ch = os.read(fd, 1)
                if ch in (b"\r", b"\n", b""):
                    return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, antigo)
    else:
        sys.stdin.readline()


def rodar_cenario(cen: Cenario, cfg: Config, console: Console, passo: bool = False,
                  animar: bool = True) -> Simulacao:
    sim = Simulacao(cen, cfg).executar()
    tela = Tela(sim, console)
    console.print(tela.cabecalho())

    if animar and console.is_terminal:
        with Live(console=console, auto_refresh=False, transient=True) as live:
            for T in range(sim.total):
                pausa = passo and T in sim.eventos_por_slot
                dica = (f" ⏸  t={T} — aperte Enter para continuar (Ctrl+C para sair) "
                        if pausa else None)
                live.update(tela.quadro_ao_vivo(T, dica), refresh=True)
                if pausa:
                    esperar_enter()
                else:
                    time.sleep(cfg.velocidade)
    tela.imprimir_final()
    return sim


# ════════════════════════════════════════════════════════════════════════
# Modo de comparação
# ════════════════════════════════════════════════════════════════════════

def comparar(cfg: Config, console: Console, quadros: int | None, semente: int | None) -> None:
    casos = [
        ("CSMA/CA normal", montar_cenario("normal", semente=semente, quadros=quadros)),
        ("Sem recuo", montar_cenario("sem-recuo", semente=semente, quadros=quadros)),
        ("Oculto, sem RTS", montar_cenario("oculto", semente=semente, quadros=quadros)),
        ("Oculto, com RTS", montar_cenario("oculto", rts=True, semente=semente, quadros=quadros)),
    ]
    desc = f"{quadros} quadros por estação" if quadros else "execuções curtas"
    tab = Table(title=f"Comparação dos cenários ({desc})", box=box.HEAVY_HEAD, title_style="bold")
    for nome in ("Cenário", "Entregues", "Colisões", "Descartados", "Slots", "Uso do canal"):
        tab.add_column(nome, justify="left" if nome == "Cenário" else "right")
    for nome, cen in casos:
        s = Simulacao(cen, cfg).executar()
        cor = "red" if s.descartados else None
        tab.add_row(nome, f"{s.entregues} / {s.quadros}", str(s.colisoes),
                    Text(str(s.descartados), style=cor or ""), str(s.total),
                    f"{round(100 * s.uso)}%")
    console.print(tab)
    console.print(Text(" Colisões = eventos de colisão no AP.  Uso do canal = slots com dados "
                       "entregues com sucesso ÷ total de slots.", style="dim"))


# ════════════════════════════════════════════════════════════════════════
# Menu interativo e linha de comando
# ════════════════════════════════════════════════════════════════════════

def menu(cfg: Config, console: Console) -> None:
    opcoes = [
        ("1", "Cenário normal (animado)", lambda: rodar_cenario(montar_cenario("normal"), cfg, console)),
        ("2", "Cenário normal, passo a passo",
         lambda: rodar_cenario(montar_cenario("normal"), cfg, console, passo=True)),
        ("3", "E se não houvesse recuo?",
         lambda: rodar_cenario(montar_cenario("sem-recuo"), cfg, console)),
        ("4", "Terminal oculto, sem RTS/CTS",
         lambda: rodar_cenario(montar_cenario("oculto"), cfg, console)),
        ("5", "Terminal oculto, com RTS/CTS",
         lambda: rodar_cenario(montar_cenario("oculto", rts=True), cfg, console)),
        ("6", "Tabela comparativa (cenários longos)", lambda: comparar(cfg, console, 20, None)),
    ]
    while True:
        console.clear()
        texto = "\n".join(f" [bold cyan]{k}[/]  {d}" for k, d, _ in opcoes) + "\n [bold cyan]0[/]  Sair"
        console.print(Panel(texto, title="Simulador CSMA/CA (802.11)", box=box.ROUNDED,
                            expand=False, padding=(1, 2)))
        escolha = Prompt.ask("Qual cenário rodar?", choices=[k for k, _, _ in opcoes] + ["0"],
                             default="1", console=console)
        if escolha == "0":
            return
        console.clear()
        dict((k, f) for k, _, f in opcoes)[escolha]()
        console.print()
        console.input("[dim]Enter para voltar ao menu…[/]")


def _ocultos(texto: str) -> frozenset:
    pares = set()
    for item in texto.split(","):
        item = item.strip()
        if not item:
            continue
        partes = item.replace(" ", "").split("-")
        if len(partes) != 2 or not all(partes):
            raise argparse.ArgumentTypeError(f"par inválido: {item!r} (use, p.ex., A-C)")
        pares.add(_par(partes[0].upper(), partes[1].upper()))
    return frozenset(pares)


def _positivo(texto: str) -> int:
    v = int(texto)
    if v < 1:
        raise argparse.ArgumentTypeError("deve ser ≥ 1")
    return v


def _nao_negativo(texto: str) -> int:
    v = int(texto)
    if v < 0:
        raise argparse.ArgumentTypeError("deve ser ≥ 0")
    return v


def criar_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="csma.py",
        description="Simulador do protocolo CSMA/CA (IEEE 802.11) com linha do tempo animada. "
                    "Sem argumentos, abre um menu interativo.")
    p.add_argument("--cenario", choices=sorted(CENARIOS), help="cenário a simular (padrão: normal)")
    p.add_argument("--semente", type=int, help="semente do sorteio (padrão: a do cenário)")
    p.add_argument("--passo", action="store_true", help="pausa a cada evento (Enter para seguir)")
    p.add_argument("--rts", action="store_true", help="ativa RTS/CTS")
    p.add_argument("--comparar", action="store_true", help="tabela comparando os cenários")
    p.add_argument("--quadros", type=_positivo, default=20,
                   help="quadros por estação no modo --comparar (padrão: 20)")
    p.add_argument("--curto", action="store_true",
                   help="no modo --comparar, usa as execuções curtas da demonstração")
    p.add_argument("--estatico", action="store_true", help="sem animação: imprime o resultado direto")
    p.add_argument("--ocultos", type=_ocultos, metavar="PARES",
                   help="pares de estações que não se ouvem, p.ex. A-C ou A-C,B-C")

    g = p.add_argument_group("parâmetros do protocolo")
    g.add_argument("--sifs", type=_positivo, default=1, help="duração do SIFS em slots (1)")
    g.add_argument("--difs", type=_positivo, default=2, help="duração do DIFS em slots (2)")
    g.add_argument("--dados", type=_positivo, help="duração dos DADOS em slots (4; 6 no oculto)")
    g.add_argument("--ack", type=_positivo, default=1, help="duração do ACK em slots (1)")
    g.add_argument("--faixa-inicial", type=_nao_negativo, default=3,
                   help="faixa inicial de recuo 0..N (3)")
    g.add_argument("--faixa-maxima", type=_nao_negativo, default=31,
                   help="faixa máxima de recuo 0..N (31)")
    g.add_argument("--max-retx", type=_nao_negativo, default=5,
                   help="máximo de retransmissões antes de descartar (5)")
    g.add_argument("--velocidade", type=float, default=0.3,
                   help="segundos por slot na animação (0.3)")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = criar_parser().parse_args(argv)
    cfg = Config(sifs=args.sifs, difs=args.difs, dados=args.dados, ack=args.ack,
                 cw_min=args.faixa_inicial, cw_max=max(args.faixa_maxima, args.faixa_inicial),
                 max_retx=args.max_retx, velocidade=max(0.0, args.velocidade))
    console = Console(highlight=False)
    try:
        if not argv:
            menu(cfg, console)
        elif args.comparar:
            comparar(cfg, console, None if args.curto else args.quadros, args.semente)
        else:
            cen = montar_cenario(args.cenario or "normal", rts=args.rts, semente=args.semente,
                                 ocultos=args.ocultos)
            rodar_cenario(cen, cfg, console, passo=args.passo, animar=not args.estatico)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrompido.[/]")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
