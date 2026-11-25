# F19 architecture styles

Three services built in recognisable architecture styles, with antipattern conditions planted on purpose:

- `services/orders`: a layered FastAPI service (api → services → repositories), with separate command and query modules (CQRS) and a saga that cancels an order when payment fails. Its inventory client retries with no backoff.
- `services/payments`: a hexagonal worker (adapters → ports and core). Its gateway adapter retries with exponential backoff; its notifier fans out with no concurrency limit.
- `services/assistant`: an Express service whose agent loop calls a model and runs tools with no iteration bound, fans out with `Promise.all` and retries with no delay.
