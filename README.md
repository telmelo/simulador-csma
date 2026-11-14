# Simulador CSMA/CA (802.11)

Simulador de terminal do protocolo de acesso ao meio **CSMA/CA** do padrão **IEEE 802.11** (Wi-Fi),
com uma **linha do tempo animada** que mostra, slot a slot, cada passo do protocolo.

(Kurose & Ross, *Redes de Computadores e a Internet*, Seções 7.1–7.3).

```
 t     0         5         10        15        20        25        30        35        40        45
 A     d d ████████· · d d 2 - - - - d d 1 ████████· · · · · · · · · · · · · · · · · · · · · · · · ·
 B     · · - - - - d - d d 1 XXXXXXXX· · d - - - - d - d d 7 - - - - d - d d 6 5 4 3 2 1 ████████· ·
 C     · · · - - - d - d d 1 XXXXXXXX· · d - - - - d - d d 1 ████████· · · · · · · · · · · · · · · ·
 AP    · · · · · · · AK· · · · · · · · · · · · · · · AK· · · · · · · · AK· · · · · · · · · · · · · AK
 Canal     ████████  AK      XXXXXXXX      ████████  AK      ████████  AK                ████████  AK
```

## O que o simulador mostra

- espera do **DIFS** e do **SIFS**, e por que o ACK tem prioridade (SIFS < DIFS);
- **recuo aleatório** (*backoff*) com contagem regressiva e **congelamento** quando o canal fica ocupado;
- **ACK na camada de enlace** e retransmissão quando o ACK não chega;
- **colisões** e o **recuo exponencial binário** (a faixa de sorteio dobra a cada falha);
- o problema do **terminal oculto** e a solução com **RTS/CTS** (silêncio por reserva, ou NAV).

Ao final de cada execução aparece um resumo com estatísticas. O modo de comparação reúne os
resultados dos cenários numa tabela.

## Requisitos

- Python 3.10 ou superior
- Biblioteca [`rich`](https://github.com/Textualize/rich)

Roda em Linux, macOS e Windows, sem depender de rede nem de hardware.

## Instalação

```bash
git clone <url-do-repositório>
cd csma
pip install -r requirements.txt
```

## Uso

### Menu interativo

```bash
python csma.py
```

Sem argumentos, o programa abre um menu que pergunta qual cenário rodar. É a forma mais prática
de usar durante uma apresentação.

### Linha de comando

```bash
python csma.py --cenario normal --semente 3           # animação slot a slot
python csma.py --cenario normal --semente 3 --passo   # pausa a cada evento (Enter para seguir)
python csma.py --cenario sem-recuo                    # "e se não houvesse recuo?"
python csma.py --cenario oculto                       # terminal oculto, sem RTS/CTS
python csma.py --cenario oculto --rts                 # mesmo cenário, com RTS/CTS
python csma.py --comparar                             # tabela comparativa (cenários longos)
```

### Opções

| Opção | Descrição | Padrão |
|---|---|---|
| `--cenario {normal,sem-recuo,oculto}` | Cenário a simular | `normal` |
| `--semente N` | Semente do sorteio. A mesma semente sempre gera a mesma execução. | 3 (normal, sem-recuo), 6 (oculto) |
| `--passo` | Pausa a cada evento e só avança com Enter | — |
| `--rts` | Ativa RTS/CTS | — |
| `--comparar` | Imprime a tabela comparativa dos cenários | — |
| `--quadros N` | Quadros por estação no modo `--comparar` | 20 |
| `--curto` | No `--comparar`, usa as execuções curtas da demonstração | — |
| `--estatico` | Sem animação: imprime o resultado direto | — |
| `--velocidade S` | Segundos por slot na animação | 0.3 |
| `--ocultos PARES` | Pares de estações que não se ouvem, ex.: `A-C` ou `A-C,B-C` | `A-C` no cenário oculto |
| `--sifs`, `--difs`, `--dados`, `--ack` | Durações em slots | 1, 2, 4 (6 no oculto), 1 |
| `--faixa-inicial`, `--faixa-maxima` | Faixa do recuo (0..N) | 3, 31 |
| `--max-retx` | Retransmissões antes de descartar o quadro | 5 |

Durante a execução, **Ctrl+C** interrompe o programa.

## Cenários

| Cenário | Estações (chegada) | Quadros | DADOS | O que evidencia |
|---|---|---|---|---|
| `normal` | A (t=0), B (t=2), C (t=3) | A=2, B=1, C=1 | 4 | CSMA/CA completo: DIFS, recuo, congelamento, colisão por sorteio igual, faixa que dobra |
| `sem-recuo` | A (t=0), B (t=2), C (t=3) | A=2, B=1, C=1 | 4 | Sem recuo, as estações ficam sincronizadas, colidem toda vez e descartam os quadros |
| `oculto` | A (t=0), C (t=3) | A=1, C=1 | 6 | A e C não se ouvem: C transmite no meio da transmissão de A e os quadros colidem no AP |
| `oculto --rts` | A (t=0), C (t=3) | A=1, C=1 | 6 | O CTS do AP é ouvido por todos: C fica em silêncio e não há colisão de dados |

As sementes padrão reproduzem as saídas de referência usadas na apresentação.

## Como ler a linha do tempo

Cada linha é uma estação (e, no fim, o AP e o canal). Cada coluna é um slot de tempo.

| Símbolo | Significado |
|---|---|
| `d` | Esperando o DIFS (canal livre) |
| `3 2 1` | Contador do recuo descendo. O número é o valor antes do decremento: "1" quer dizer que este é o último slot de espera. |
| `-` | Canal ocupado: estação esperando ou com o contador congelado |
| `██` | Transmitindo DADOS |
| `XX` | DADOS que colidiram |
| `AK` | ACK enviado pelo AP |
| `RT` / `CT` | RTS / CTS |
| `░░` | Em silêncio por reserva (ouviu o CTS) |
| `·` | Nada a fazer, ou aguardando o ACK |

Abaixo da grade aparecem o **registro de eventos** (em português) e o **resumo da execução**.

## Modelo simplificado

O tempo avança em **slots** discretos. Todas as estações enviam quadros ao **ponto de acesso (AP)**
pelo mesmo canal, e todas ouvem o AP. No modo terminal oculto, os pares configurados não se ouvem
entre si.

Cada estação segue esta máquina de estados:

| Estado | O que faz | Próximo estado |
|---|---|---|
| Ociosa | Não tem quadro para enviar | Chega quadro com canal livre: DIFS sem recuo. Com canal ocupado: sorteia recuo e vai ao DIFS. |
| DIFS | Espera o canal ficar livre por DIFS slots seguidos | Se o canal ocupa, recomeça o DIFS (e sorteia recuo, se ainda não tinha). Ao completar, vai ao recuo ou transmite. |
| Recuo | Conta o recuo regressivamente, só com o canal livre | Se o canal ocupa, congela o contador e volta ao DIFS. Se o contador zera, transmite. |
| Transmitindo | Envia DADOS (ou RTS) | Ao terminar, vai a Esperando ACK (ou CTS). |
| Esperando ACK | Aguarda SIFS + ACK | Se o ACK chega, é sucesso e a faixa volta a 0–3. Se não chega, a faixa dobra e a estação sorteia de novo. Depois de 5 retransmissões, descarta o quadro. |

Em cada slot, a simulação processa nesta ordem:

1. inicia as transmissões agendadas;
2. registra as chegadas de quadros;
3. faz o sensoriamento do canal e atualiza DIFS, recuo e congelamento;
4. trata o fim das transmissões (o AP decide entre ACK/CTS e colisão);
5. trata sucessos e timeouts.

O AP envia ACK (ou CTS) um SIFS depois de receber um quadro sem sobreposição. Se dois quadros se
sobrepõem no AP, há colisão e o AP não responde. O silêncio por reserva (NAV) começa quando o CTS
é transmitido e dura até o fim do ACK correspondente.

## Estrutura

```
csma/
├── csma.py            # simulador (arquivo único)
├── requirements.txt   # dependência: rich
└── README.md
```

## Referência

KUROSE, J. F.; ROSS, K. W. *Redes de Computadores e a Internet: uma abordagem top-down*.
Capítulo 7: Redes sem fio e redes móveis (Seções 7.1–7.3).
