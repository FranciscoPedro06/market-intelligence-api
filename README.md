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

**Entrada — C2 `v1.0.0`** (`docs/ecosystem/contracts.md`). Grão: rota **direcional**
(origem→destino) × companhia × mês, com `route_pair_id` não-direcional. A API lê
**verbatim**: `on_time_rate`, `flights_operated`, `flights_on_time`, contadores de
transparência (`flights_cancelled`, `flights_not_reported`,
`flights_operated_missing_arrival`, `flights_source_total`) e a linhagem/proveniência.

**Saída — C3-draft (Fase 1).** Comparação por rota, por mês, ordenada melhor→pior:

| Campo servido | Origem |
|---|---|
| `on_time_rate`, `flights_operated`, `flights_on_time` | **lido verbatim do C2** |
| `transparency.*` (4 contadores) | **lido verbatim do C2** |
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
| `D1_CONTRACT_NAME` / `D2_CONTRACT_VERSION` | é C2, e numa versão suportada (`v1.0.0`) |
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
| `R7_SOURCE_TOTAL` | `flights_source_total` fecha a reconciliação (AC4) |
| `R8_ROUTE_KEY` / `R8_PAIR_KEY` / `R8_MISSING_IATA` | chaves de rota coerentes com as colunas ICAO; colunas IATA presentes |
| `R9_MONTH_FORMAT` | `reference_month` em `YYYY-MM` |

**Gates de rastreabilidade → avisam e continuam servindo** (`status: pass_with_warnings`):
`P1_PROVENANCE`, `P2_LINEAGE`, `P3_AUDIT`, `P4_AIRLINE_NAME`, `R9_MONTH_LINEAGE`. O
número continua sendo o do C2; só a trilha de auditoria está degradada.

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
Default de `--input`: `input/c2_punctuality.json` (o C2 real, ainda não disponível na Fase 1).

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
python tests/self_test.py        # 40 casos, stdlib unittest, sem dependências
```

## Exemplo de saída (fixture sintética `input/sample_c2.json`)

```
Route pair: CGH-SDU   month: 2023-07   mode: per-direction
Metric: pontualidade v1.0.0 | basis=arrival threshold=15 min | C2=v1.0.0
C2 validation: pass (12 valid, 0 quarantined, 0 error, 0 warning)
! SYNTHETIC C2 INPUT - illustrative numbers, not real ANAC/VRA data.

=== 2023-07 ===
  Direction CGH->SDU (best -> worst):
    #  airline  on_time_rate   operated  on_time   canc  n/r  miss  src_total
    1  GLO      85.88%        255      219      5    1     2        263
    2  TAM      83.87%        310      260      8    2     6        326
    3  AZU       n/a            0        0     12    3     0         15
    => most reliable: GLO at 85.88% (+2.01 pp over TAM)  [no data: AZU]
  Direction SDU->CGH (best -> worst):
    #  airline  on_time_rate   operated  on_time   canc  n/r  miss  src_total
    1  GLO      84.68%        248      210      4    1     3        256
    2  TAM      84.59%        305      258      6    2     4        317
    3  AZU      71.00%        100       71      3    1     2        106
    => most reliable: GLO at 84.68% (+0.09 pp over TAM)
```
(Percentuais são só formatação de exibição; o JSON carrega o `on_time_rate` em
precisão plena do C2 — arredondamento é responsabilidade do C3. `AZU` aparece com
`n/a` porque o denominador do C2 é 0: os 15 voos existem como cancelados/não
informados e continuam visíveis nos contadores de transparência.)

## Self-test / fixture
`input/sample_c2.json` é **SINTÉTICO** (marcado com `_synthetic` e `_warning`),
conforme ao C2 `v1.0.0`: 3 companhias (TAM/GLO/AZU), 2 meses (2023-06/07), ambas as
direções, incluindo um caso `on_time_rate = null` (denominador 0) para exercitar a
transparência. Commitado via force-add (o `.gitignore` ignora `input/`).

`tests/self_test.py` (40 casos) verifica, em ordem de importância:

1. **RT5 / AC4** — o `on_time_rate` servido **é** o objeto do C2 (`assertIs`, não
   comparação aproximada) nos 12 registros; contagens e os 4 contadores de
   transparência copiados verbatim.
2. **Cada gate de validação** dispara sob uma corrupção dirigida da fixture — um gate
   que nunca dispara não é um gate. Verifica também o raio de impacto: gate de
   documento não serve nada, gate de registro retém exatamente 1, aviso serve os 12.
3. **Nulos e exclusões** sobrevivem até a resposta em vez de virarem 0.
4. **AC5 determinismo** — JSON idêntico entre recargas; a ordem dos registros de
   entrada não muda o ranking.
5. **Validação de pedido** e semântica de status HTTP (matriz de 18 casos); a saída
   HTTP é idêntica à chamada em processo para a mesma query.

## Estado
Sprint 1 — Walking Skeleton, Fase 1. Camada de exposição funcional e validada sobre
C2, com a pergunta-âncora respondida explicitamente.
Armazenamento/serialização/framework permanecem deferidos.

**Pendente para a Fase 2/3** (depende de outros produtos, não desta camada): trocar a
fixture sintética pelo C2 real do Analytics (AC1/AC2), reconciliação manual conjunta
contra o VRA (AC4) e revisão cruzada entre os três engenheiros (DoD).

Contratos e governança:
https://github.com/FranciscoPedro06/Market-Intelligence-Ecosystem
