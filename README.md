# market-intelligence-api

Parte do **Market Intelligence Ecosystem** · Produto de dados: *Flight Intelligence Platform*.

## Papel neste ecossistema
Camada de **exposição** (thin read-only): serve o indicador de pontualidade já
calculado para responder à pergunta-âncora — *"na rota CGH↔SDU, qual companhia é
mais confiável?"*. **Não contém regra de cálculo (RT5).**

> **RT5 — a API não calcula nada.** A janela de 15 min, o denominador, o mapa
> ICAO→IATA e o próprio `on_time_rate` são resolvidos no **C2** (Analytics). Aqui
> apenas: **lê** C2 → **valida** contra o contrato → **filtra** por rota/mês → agrupa
> as duas direções → lê os campos prontos → ordena melhor-para-pior → **devolve** com
> proveniência. Sem DB, sem framework web, sem orquestrador (stdlib apenas: `json`,
> `argparse`, `http.server`, `re`).

## Contrato: C2 entra → resposta (C3-draft) sai

**Entrada — C2 `v1.1.0`** (`docs/ecosystem/contracts.md`; `v1.0.0` continua aceito —
a emenda é aditiva, nenhum campo foi removido ou renomeado). Grão: rota **direcional**
(origem→destino) × companhia × mês, com `route_pair_id` não-direcional.

**A API serve os números verbatim.** Ela não calcula, não deriva e não corrige nada:
`on_time_rate`, `flights_operated`, `flights_on_time`, os **5** contadores de
transparência (`flights_cancelled`, `flights_not_reported`,
`flights_operated_missing_arrival`, `flights_operated_missing_schedule`,
`flights_source_total`) e a linhagem/proveniência saem exatamente como o C2 os
escreveu — garantido por teste com `assertIs`, não por comparação aproximada.

> **Voos não mensuráveis podem aparecer com `on_time_rate = null`.** No C2 `v1.1.0` o
> denominador exige `scheduled_arrival`: um voo `REALIZADO` com chegada real mas **sem
> chegada prevista** tem pontualidade **indefinida**, fica **fora do denominador** e é
> contado em `flights_operated_missing_schedule` — **nunca** como atrasado. Quando
> todos os voos de uma companhia caem nesse caso, o denominador é 0 e o C2 emite
> `on_time_rate = null`. A API **repassa esse null**: não o converte em 0 (que
> afirmaria 0% de pontualidade), não ranqueia a companhia e a lista em
> `excluded_no_denominator`. Os voos continuam visíveis nos contadores, e
> `flights_source_total` prova que nenhuma linha do C1 desapareceu.

**Saída — C3-draft (Fase 1).** Comparação por rota, por mês, ordenada melhor→pior:

| Campo servido | Origem |
|---|---|
| `on_time_rate`, `flights_operated`, `flights_on_time` | **lido verbatim do C2** |
| `transparency.*` (5 contadores, incl. `flights_operated_missing_schedule`) | **lido verbatim do C2** |
| `provenance` (`metric_version`, `on_time_basis`, `on_time_threshold_minutes`, `source_lineage`, …) | **lido verbatim do C2** |
| `rank`, ordenação melhor→pior | derivado (apenas ordenação — não é lógica de métrica) |
| `answer` / `answers` (companhia mais confiável) | derivado (apenas a leitura da ordenação) |
| `rate_gap_vs_runner_up` | derivado (subtração de dois valores do C2, para exibição) |
| `validation` | resultado da checagem do contrato C2 (não produz valor servido) |
| modo `--combine`: soma de contagens das 2 direções | **agregação pura** de inteiros do C2 (soma numeradores / soma denominadores), documentada; **não** re-executa a métrica |

Regra de nulos preservada: `on_time_rate = null` quando denominador 0 → exibido como
`n/a`, ordenado por último e **nunca vence** a comparação. Nada é inventado.

### As duas direções do par ↔
Por padrão a resposta é **por direção** (`CGH->SDU` e `SDU->CGH` separadas) — cada
direção mede a chegada ao seu destino, então misturá-las conflaria duas operações.
As duas direções são reunidas por `route_pair_id`. Com `--combine`, os **contadores**
(numerador e denominador) das duas direções são **somados** e a razão é reexpressa a
partir dessas somas — agregação pura de inteiros do C2, claramente rotulada, **não**
uma reaplicação da regra dos 15 min.

### A resposta à pergunta-âncora
Cada bloco de mês carrega um `answer` (modo `--combine`) ou um `answers` por direção,
derivado **somente** da ordenação do `on_time_rate` do C2. A API se recusa a responder
quando responder seria inventar:

| Situação | O que a API devolve |
|---|---|
| Um líder claro | `most_reliable`, `runner_up`, `rate_gap_vs_runner_up`, `conclusive: true` |
| `on_time_rate` nulo (denominador 0) | companhia **excluída** e listada em `excluded_no_denominator` — ausência de medição não é um nível de confiabilidade |
| Empate exato no `on_time_rate` | `most_reliable: null` + `tie: [...]` — o C2 não oferece critério de desempate, e criar um seria regra de negócio |
| Menos de 2 companhias comparáveis | `conclusive: false` — o AC3 pede uma *comparação* |
| Todas as taxas nulas | `most_reliable: null`, `conclusive: false` |

## Validação do C2 (o passo "valida")

A API **não confia** no input: `src/c2_validation.py` gateia o documento antes de
servir. Os checks apenas **asseguram o contrato** — nenhum valor servido nasce aqui.

**Gates de documento → recusam o documento inteiro** (`status: refused`, nada é servido):

| Código | Verifica |
|---|---|
| `D1_CONTRACT_NAME` / `D2_CONTRACT_VERSION` | é C2, e numa versão suportada (`v1.0.0`, `v1.1.0`) |
| `D3_GRAIN_DUPLICATE` | no máximo um registro por (`route_id`, `airline_icao`, `reference_month`) |
| `D4_PROVENANCE_MIXED` | todos concordam em `metric_id`/`metric_version`/`on_time_basis`/`on_time_threshold_minutes` — ranquear números feitos por regras diferentes não significaria nada |

**Gates de registro → põem em quarentena só o registro ruim** (`status: quarantined`;
o registro é **listado no relatório**, nunca descartado em silêncio):

| Código | Verifica |
|---|---|
| `R1_MISSING_REQUIRED` / `R2_TYPE` | campos obrigatórios do C2 presentes e tipados |
| `R3_NEGATIVE_COUNT` / `R3_NUMERATOR_GT_DENOMINATOR` | contagens ≥ 0 e numerador ≤ denominador |
| `R4_NULL_RULE` | `on_time_rate` é nulo **exatamente quando** o denominador é 0 (nunca 0/0) |
| `R5_RATE_RANGE` | taxa em [0,1] (fração, não percentual) |
| `R6_RATE_INCONSISTENT` | a taxa servida é a razão das contagens servidas |
| `R7_SOURCE_TOTAL` | `flights_source_total` fecha a reconciliação (AC4) sobre os 4 buckets fora do denominador — em `v1.1.0` inclui `flights_operated_missing_schedule`; ausente (`v1.0.0`) ele contribui 0, então uma só fórmula serve as duas versões |
| `R8_ROUTE_KEY` / `R8_PAIR_KEY` / `R8_MISSING_IATA` | chaves de rota coerentes com as colunas ICAO; colunas IATA presentes |
| `R9_MONTH_FORMAT` | `reference_month` em `YYYY-MM` |

**Gates de rastreabilidade → avisam e continuam servindo** (`status: pass_with_warnings`):
`P1_PROVENANCE`, `P2_LINEAGE`, `P3_AUDIT`, `P4_AIRLINE_NAME`, `R9_MONTH_LINEAGE` e
`P5_MISSING_V110_COUNTER` (`metric_version = v1.1.0` sem os contadores da v1.1.0 — só
aviso, porque o `R7` acima já pega, como erro, uma soma que deixou de fechar). O
número continua sendo o do C2; só a trilha de auditoria está degradada.

**Versões conviventes.** Um contador introduzido numa versão posterior é
legitimamente **ausente** num documento antigo — isso não é erro. A API o serve como
`null` (exibido `-` na tabela), nunca como `0`: zero afirmaria "nenhum voo nesse
bucket", e o C2 `v1.0.0` nunca disse isso. O mesmo vale ao combinar direções — somar
contadores ausentes não fabrica um `0`.

> **RT5 no gate `R6`:** ele divide numerador por denominador *apenas para conferir* o
> `on_time_rate` do C2 e descarta o resultado. O valor servido é sempre o que o C2
> escreveu — garantido por teste com `assertIs`.

## Endpoints (HTTP, stdlib `http.server`, read-only)

| Método | Rota | Devolve |
|---|---|---|
| `GET` | `/` | índice dos endpoints |
| `GET` | `/health` | liveness, registros carregados, status da validação do C2 |
| `GET` | `/meta` | versões de contrato (in/out), proveniência da métrica, cobertura |
| `GET` | `/validation` | relatório completo de validação do C2 |
| `GET` | `/routes` | pares de rota disponíveis neste C2 (meses, companhias, direções) |
| `GET` | `/routes/{PAR}/airlines` | companhias presentes no par |
| `GET` | `/routes/{PAR}/punctuality?month=YYYY-MM&combine=true` | comparação completa + resposta-âncora |
| `GET` | `/routes/{PAR}/most-reliable?month=YYYY-MM&combine=true` | só a resposta-âncora |

`{PAR}` aceita **IATA** (`CGH-SDU`) ou **ICAO** (`SBSP-SBRJ`), em qualquer ordem e
caixa — a filtragem usa a família de colunas que o chamador escolheu; a API **não
traduz** códigos (tradução é do Analytics).

**Códigos de status.** Um pedido que a API não pode responder nunca se parece com uma
resposta:

| Status | Quando |
|---|---|
| `400` | par de rota malformado, `month` fora de `YYYY-MM`/01-12, `combine` não-booleano |
| `404` | pedido bem-formado, mas o par/mês não existe no C2 — o corpo lista `available_route_pairs` / `available_months` |
| `405` | `POST`/`PUT`/`PATCH`/`DELETE` (com header `Allow: GET, HEAD`) |
| `503` | sem relatório de validação disponível |

## Como rodar (Python 3, stdlib)

**CLI:**
```bash
python src/serve.py --input input/sample_c2.json --route CGH-SDU --month 2023-06
python src/serve.py --input input/sample_c2.json --route CGH-SDU --combine   # soma as 2 direções
python src/serve.py --input input/sample_c2.json --route CGH-SDU --json      # resposta JSON crua
python src/serve.py --input input/sample_c2.json --validate                  # relatório de validação do C2
python src/serve.py --input input/sample_c2.json --validate --strict         # sai != 0 se houver erro
```
Default de `--input`: `input/c2_punctuality.json`. O C2 real produzido pelo Analytics
fica em `../market-intelligence-analytics/output/c2_on_time.csv` (conteúdo JSON) —
passe-o com `--input` ou copie-o para `input/c2_punctuality.json`:

```bash
python src/serve.py --input ../market-intelligence-analytics/output/c2_on_time.csv \
    --route CGH-SDU --month 2023-06
```

**Códigos de saída da CLI:** `0` ok · `2` input inexistente/ilegível ou pedido
malformado · `3` C2 recusado pela validação (ou `--strict` com erros) · `4` rota/mês
não existe no C2.

**HTTP:**
```bash
python src/serve.py --input input/sample_c2.json --http --port 8000
curl http://127.0.0.1:8000/routes
curl "http://127.0.0.1:8000/routes/CGH-SDU/punctuality?month=2023-06"
curl "http://127.0.0.1:8000/routes/CGH-SDU/most-reliable?month=2023-07&combine=true"
curl http://127.0.0.1:8000/validation
```

**Self-test:**
```bash
python tests/self_test.py        # 54 casos, stdlib unittest, sem dependências
```

## Exemplo de saída (C2 `v1.1.0` real do Analytics)

```
$ python src/serve.py --input ../market-intelligence-analytics/output/c2_on_time.csv \
      --route CGH-SDU --month 2023-06

Route pair: CGH-SDU   month: 2023-06   mode: per-direction
Metric: pontualidade v1.1.0 | basis=arrival threshold=15 min | C2=None
C2 validation: pass_with_warnings (25 valid, 0 quarantined, 0 error, 2 warning)

=== 2023-06 ===
  Direction SDU->CGH (best -> worst):
    #  airline  on_time_rate   operated  on_time   canc  n/r  no_arr  no_sch  src_total
    1  TAM      86.75%        649      563     26    0      0      2        677
    2  GLO      85.61%        528      452     38    0      0      0        566
    3  AZU      83.04%        336      279      1    0      0      0        337
    4  ACN       n/a            0        0      0    0      0      4          4
    => most reliable: TAM at 86.75% (+1.14 pp over GLO)  [no data: ACN]
  Direction CGH->SDU (best -> worst):
    #  airline  on_time_rate   operated  on_time   canc  n/r  no_arr  no_sch  src_total
    1  TAM      88.67%        653      579     22    0      0      0        675
    2  AZU      87.42%        326      285     10    0      0      8        344
    3  GLO      85.09%        530      451     36    0      0      0        566
    4  ACN       n/a            0        0      0    0      0      4          4
    => most reliable: TAM at 88.67% (+1.24 pp over AZU)  [no data: ACN]
```

`ACN` é exatamente o caso de **voo não mensurável**: os 4 voos existem e aparecem em
`no_sch` (`flights_operated_missing_schedule`), fecham o `src_total`, mas ficam fora
do denominador — então o C2 emite `on_time_rate = null`, a API exibe `n/a` e a
companhia é excluída da resposta em vez de ranqueada como pior. `AZU` mostra o caso
misto: 8 voos sem previsão de chegada, e ainda assim uma taxa válida sobre os 326
mensuráveis.

Percentuais são só formatação de exibição; o JSON carrega o `on_time_rate` em precisão
plena do C2 — arredondamento é responsabilidade do C3. Coluna `-` significa contador
não reportado por aquela versão do C2 (ver *Versões conviventes*). `C2=None` e os 2
avisos vêm de o arquivo do Analytics ser um array JSON puro, sem envelope declarando
`contract`/`contract_version`; os registros declaram `metric_version: v1.1.0`.

## Self-test / fixture
`input/sample_c2.json` é **SINTÉTICO** (marcado com `_synthetic` e `_warning`),
conforme ao C2 `v1.0.0`: 3 companhias (TAM/GLO/AZU), 2 meses (2023-06/07), ambas as
direções, incluindo um caso `on_time_rate = null` (denominador 0) para exercitar a
transparência. Commitado via force-add (o `.gitignore` ignora `input/`).

`tests/self_test.py` (54 casos) verifica, em ordem de importância:

1. **RT5 / AC4** — o `on_time_rate` servido **é** o objeto do C2 (`assertIs`, não
   comparação aproximada) nos 12 registros; contagens e os contadores de
   transparência copiados verbatim.
2. **Cada gate de validação** dispara sob uma corrupção dirigida da fixture — um gate
   que nunca dispara não é um gate. Verifica também o raio de impacto: gate de
   documento não serve nada, gate de registro retém exatamente 1, aviso serve os 12.
3. **Nulos e exclusões** sobrevivem até a resposta em vez de virarem 0.
4. **AC5 determinismo** — JSON idêntico entre recargas; a ordem dos registros de
   entrada não muda o ranking.
5. **Validação de pedido** e semântica de status HTTP (matriz de 18 casos); a saída
   HTTP é idêntica à chamada em processo para a mesma query.
6. **Alinhamento C2 `v1.1.0`** — o novo contador servido verbatim, somado ao combinar
   direções, incluído no gate `R7`; ausência em `v1.0.0` servida como `null` e não `0`;
   e o caso de voo não mensurável (denominador 0 → `on_time_rate = null`, companhia
   excluída da resposta).
7. **Contra o C2 real do Analytics**, quando presente em
   `../market-intelligence-analytics/output/c2_on_time.csv` (a classe é *skipped* se o
   arquivo não existir, para o suite não depender de um repo vizinho): validação sem
   erros, `metric_version = v1.1.0` em todos os registros, reconciliação dos 5 buckets
   e o caso `ACN` (`on_time_rate = null` com `flights_operated_missing_schedule > 0`
   nos 6 registros servidos).

## Estado
Sprint 1 — Walking Skeleton. Camada de exposição funcional e validada sobre C2
`v1.1.0`, com a pergunta-âncora respondida explicitamente.
Armazenamento/serialização/framework permanecem deferidos.

Alinhada ao C2 `v1.1.0` em 2026-07-25 e verificada contra a saída real do Analytics
(25 registros, 5 companhias, 2023-04..06, 0 erro de contrato).

**Pendente para a Fase 3** (depende dos três engenheiros, não desta camada):
reconciliação manual conjunta contra o VRA (AC4) e revisão cruzada (DoD).

**Observação para o Analytics** (não bloqueante): a saída é um array JSON puro, sem
envelope `{"contract": "C2", "contract_version": "v1.1.0", "records": [...]}`, então a
API avisa que não pode confirmar a versão do contrato pelo documento — só pelo
`metric_version` de cada registro. Além disso `output/c2_on_time.csv` tem conteúdo
**JSON**, não CSV, apesar da extensão.

Contratos e governança:
https://github.com/FranciscoPedro06/Market-Intelligence-Ecosystem
