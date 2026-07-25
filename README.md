# market-intelligence-api

Parte do **Market Intelligence Ecosystem** · Produto de dados: *Flight Intelligence Platform*.

## Papel neste ecossistema
Camada de **exposição** (thin read-only): serve o indicador de pontualidade já
calculado para responder à pergunta-âncora — *"na rota CGH↔SDU, qual companhia é
mais confiável?"*. **Não contém regra de cálculo (RT5).**

> **RT5 — a API não calcula nada.** A janela de 15 min, o denominador, o mapa
> ICAO→IATA e o próprio `on_time_rate` são resolvidos no **C2** (Analytics). Aqui
> apenas: carrega C2 → filtra por rota/mês → agrupa as duas direções → lê os campos
> prontos → ordena melhor-para-pior → anexa proveniência. Sem DB, sem framework web,
> sem orquestrador (stdlib apenas: `json`, `argparse`, `http.server`).

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
| modo `--combine`: soma de contagens das 2 direções | **agregação pura** de inteiros do C2 (soma numeradores / soma denominadores), documentada; **não** re-executa a métrica |

Regra de nulos preservada: `on_time_rate = null` quando denominador 0 → exibido como
`n/a` e ordenado por último. Nada é inventado.

### As duas direções do par ↔
Por padrão a resposta é **por direção** (`CGH->SDU` e `SDU->CGH` separadas) — cada
direção mede a chegada ao seu destino, então misturá-las conflaria duas operações.
As duas direções são reunidas por `route_pair_id`. Com `--combine`, os **contadores**
(numerador e denominador) das duas direções são **somados** e a razão é reexpressa a
partir dessas somas — agregação pura de inteiros do C2, claramente rotulada, **não**
uma reaplicação da regra dos 15 min.

## Como rodar (Python 3, stdlib)

**CLI:**
```bash
python src/serve.py --input input/sample_c2.json --route CGH-SDU --month 2023-06
python src/serve.py --input input/sample_c2.json --route CGH-SDU --combine   # soma as 2 direções
python src/serve.py --input input/sample_c2.json --route CGH-SDU --json      # resposta JSON crua
```
Default de `--input`: `input/c2_punctuality.json` (o C2 real, ainda não disponível na Fase 1).

**HTTP (stdlib `http.server`, read-only, só GET):**
```bash
python src/serve.py --input input/sample_c2.json --http --port 8000
# GET /routes/CGH-SDU/punctuality?month=2023-06&combine=true
# GET /health
```

## Exemplo de saída (fixture sintética `input/sample_c2.json`)

```
=== 2023-06 ===
  Direction CGH->SDU (best -> worst):
    #  airline  on_time_rate   operated  on_time   canc  n/r  miss  src_total
    1  TAM      88.00%        300      264      6    2     4        312
    2  GLO      80.00%        250      200      5    1     3        259
    3  AZU      75.00%        120       90      3    0     2        125
  Direction SDU->CGH (best -> worst):
    1  TAM      85.86%        290      249      7    1     5        303
    2  GLO      80.00%        240      192      4    2     3        249
    3  AZU      70.00%        110       77      2    1     1        114
```
(Percentuais são só formatação de exibição; o JSON carrega o `on_time_rate` em
precisão plena do C2 — arredondamento é responsabilidade do C3.)

## Self-test / fixture
`input/sample_c2.json` é **SINTÉTICO** (marcado com `_synthetic` e `_warning`),
conforme ao C2 `v1.0.0`: 3 companhias (TAM/GLO/AZU), 2 meses (2023-06/07), ambas as
direções, incluindo um caso `on_time_rate = null` (denominador 0) para exercitar a
transparência. Commitado via force-add (o `.gitignore` ignora `input/`).

## Estado
Sprint 1 — Walking Skeleton, Fase 1. Camada de exposição funcional sobre C2.
Armazenamento/serialização/framework permanecem deferidos.

Contratos e governança:
https://github.com/FranciscoPedro06/Market-Intelligence-Ecosystem
