# Контракты реализации

Реализуем план мониторинга из задачи. Python backend + React/TS/Vite frontend. Все временные строки ISO без зоны, время источника. API префикс `/api`. Интервалы [from,to). Никаких искусственных числовых данных в UI.

## Внутреннее хранение (импорт)

`backend/ingest.py`: `import_dataset(files: list[Path], output_dir: Path, name: str) -> dict`. output_dir уже создан; функция пишет `observations.parquet`, `manifest.json`. Возвращает manifest.

Parquet LONG schema: `metric_id` str, `timestamp` datetime64[ns], `value` float64 nullable, `source` str (`kip`,`lims`,`pak`), `unit` str nullable, `flags` str (разделитель `|`, empty=ok), `source_file` str, `source_row` int. 18 млн наблюдений КИП допустимы; parquet пишется по источникам через pyarrow ParquetWriter, не накапливать весь LONG в RAM. NaN/invalid raw rows retain with flag invalid. Nonfinite invalid; duplicates identical collapse (record counts), conflicting same metric/time retain marked conflict. No invented units. 6-hour flatline flag from elapsed threshold onwards (not retroactive). suspicious outliers retained (not blanket 307 removal).

IDs: `avt.T1`, `ht.F26`, `lims.avt.1.CFPP`, `lims.avt.2.1.D15` (point 2.1 dots retained), `lims.ht.2.Mg.Sulfur`, `pak.ht.Mg.Sulfur`, `pak.ht.D15`. Lab metric codes retained exactly including `CloudPoint_1`, `FilterabilityLimit.T`. Sources detected by header structure/filename original. xlsx lab pairs independent, headers rows1 group/2 metric/3 unit/4 declared count, data row5. PAK pairs A:B and D:E, data row3. Tags workbook sheet КИП alternating desc/tag columns for avt,ht.

Manifest: `{id: output_dir.name, name, status:'ready', created_at, start, end, telemetry_start, telemetry_end, sources:[{kind, filename, rows, metrics, start,end, invalid_count, suspect_count, duplicate_count}], metrics:[Metric], issues:[{code,message,count,metric_id?}], assumptions:[str]}`. `Metric` `{id,label,source,unit,unit_status:'confirmed'|'inferred'|'unknown', plant:'avt'|'ht', point?:str, code, group, description?,primary?:bool, mapping_warning?:str}`. Include all observed metrics; metadata from bundled registry `backend/resources/registry.json` optional override tags file. Unknown unit display null. `primary` true only 6 main lab ht2 metrics.

## Public API

- GET /api/datasets => `{datasets: Manifest[], default_id: string|null}`
- POST /api/datasets multipart `files` repeated and optional `name` => Manifest status importing; async background import; GET dataset polls; failure status error with error text. Store immutable originals in dataset dir/raw. Parent owns route.
- GET /api/datasets/{id} => Manifest
- GET /api/datasets/{id}/metrics => `{metrics: Metric[]}` plus derived metadata
- GET /api/datasets/{id}/snapshot?at=ISO => `{at, values: SnapshotValue[], alerts: Alert[]}`
  SnapshotValue `{metric_id,value:number|null,timestamp:string|null,available_at:string|null,age_minutes:number|null,unit,flags:string[],freshness:'fresh'|'stale'|'missing',delta:number|null,reason?:string}`. Snapshot includes all metrics. KIP/PAK use last sample <=at; LIMS uses last sample whose conservative publication time `timestamp+4h<=at`. Freshness kip<=10min pak<=30min lims<=2880min is independent from publication delay. stale values retained as historical, not current truth.
- GET /api/datasets/{id}/summary?from=ISO&to=ISO&exclude_suspect=false => `{from,to,metrics:Stat[],comparison_from,comparison_to, sulfur:{lab_count,lab_exceed_count,lab_exceed_fraction,pak_observed_minutes,pak_exceed_minutes,pak_coverage_fraction,pak_suspect_minutes}, agreement:{metric_id,n,bias,mae}[]}`
  Stat `{metric_id,count,invalid_count,suspect_count,mean,median,min,max,min_at,max_at,p05,p95,std,iqr,range,first,last,first_at,last_at,change,previous_median,median_change}` numbers nullable. summary includes all metrics. No lab forward-fill aggregation. All large-row SQL backend. Suspect flags included by default except invalid/conflict/nonfinite.
- GET /api/datasets/{id}/series?metrics=commaIDs&from=ISO&to=ISO&limit=600&exclude_suspect=false => `{series:[{metric_id,points:[{timestamp,value,min,max,flags:string[],count}]}]}`. Large rows bucket median,min,max preserving peaks; lab raw events (no fill) with max limit dynamic bucket only if >limit. Include null gap points for long gaps. Full exact values available snapshot. Graph values source-separated.
- GET /api/datasets/{id}/formulas?at=ISO => `{formulas:[{id,label,plant,expression,version,status:'experimental'|'invalid'|'unresolved'|'verified',reason,inputs:[{tag,value,timestamp,flags}],substitution,result:number|null,unit}]}`. All 17 shown; numerical impl parent. No raw eval. Registry provided by Terra.
- GET /api/datasets/{id}/quality => `{sources:manifest.sources,issues:manifest.issues,assumptions:manifest.assumptions, metrics:[{metric_id,count,invalid_count,suspect_count,flatline_count,start,end}]}`
- POST /api/datasets/{id}/scenario accepts editable source sulfur, targets, horizon 0–180 min, step 15–60 min, linear model parameters, optional control changes, reservoir components and additive 0–3%. Returns the three confirmed controls, sulfur trajectory, blend quality, cost index and explicit assumptions.
- GET /api/datasets/{id}/export?metrics=commaIDs&from=ISO&to=ISO&exclude_suspect=false => raw observations CSV streaming.
- GET /api/health => `{status:'ok'}`

Derived metric IDs: `derived.sulfur_margin.lims`, `derived.sulfur_margin.pak`, `derived.sulfur_excess.lims`, `derived.sulfur_excess.pak`, `derived.avt.delta_k2`, `derived.avt.delta_k10`. Values/series/stats available same API when practical. Catalogue may include disabled flow formulas with reason (unconfirmed units). UI calculate nothing scientific beyond formatting, read API.

## Frontend requirements

### Дополнение пользователя: разные экраны момента и периода

Момент: значения кадра/последних доступных анализов, их время, возраст, дельта от предыдущего измерения; подстановка конкретных КИП в ВАК. Нет медиан, гистограмм, длительностей превышений. История за 24 часа только раскрываемым контекстом. КИП — snapshot с явным отсутствием текущего значения при просрочке; исторический последний отдельно.

Период: медианы, диапазоны, n, динамика, распределения, покрытие, длительность превышений, сравнение с предшествующим интервалом. ПАК-компаньон карточек тоже агрегат периода, не последний замер. Лабораторный паспорт — статистики внутри периода. КИП — агрегаты периода. ВАК требует открыть конкретный момент, без выдачи последнего кадра за расчёт всего периода. Клик по точке графика переводит в момент.

Дополнительные endpoints: GET distribution (`metric,from,to,exclude_suspect`) => `{metric_id,count,bins:[{from,to,count}],quartiles:[min,q25,median,q75,max]}`; GET/PUT settings => `{freshness_minutes:{kip,pak,lims}}`; GET distillation (`at`) => `{timestamp,age_minutes,points:[{fraction,temperature,metric_id}],reason}`. Фракционные точки строго одного timestamp.

Real API data only. Empty state when no dataset; import dialog supports partial files, polling status, errors. Shell sidebar pages: monitoring, recommendations, sandbox. Recommendations expose source sulfur, target, horizon, step and lag; sandbox additionally exposes the three controls and editable reservoir blend. Default last24h telemetry end; dates no timezone conversion. Snapshot graph preceding24h. Sulfur LIMS + PAK distinct. Main threshold10 control scenario not full fuel certification. Show uncertainties/units/flags and never present a model result as an operator instruction. Responsive1280/1440/mobile.
