# F23-solution-multi-repo

One application spread over two repositories and a database catalogue dump: the first
multi-repository golden fixture. `golden_check` compiles each member in a repository of
its own, loads the solution and checks `golden.json`; `tests/test_solution.py` uses the
base as its end-to-end solution. The format is described in `../README.md`.

| Path under `base/` | What it is |
|---|---|
| `.rkp/solution.json` | The solution manifest: member `web` at `web/`, member `orders` at `service/` (its tables are in `shopdb`), and the catalogue dumps as inputs |
| `.rkp/inputs/catalog/` | The `shopdb` catalogue: tables, a view, columns with keys, a routine, roles and grants |
| `web/` | A TypeScript front end calling the orders API with `fetch` |
| `service/` | A FastAPI service serving `/api/orders` and reading the `orders` table through SQLAlchemy |

What a correct load and resolution gives at the base:

- `GET /api/orders/{id}` from `web` links to the service's endpoint exactly (E3): one
  endpoint serves that path.
- `/api/orders` from `web` is a candidate (E4) for both `GET` and `POST`: `fetch` does
  not state its method to the extractor, and two endpoints serve the path.
- `/api/customers` is unmatched: no member serves it.
- The service's `orders` table, and its columns, link exactly to the catalogue's: the
  manifest says the service's tables are in `shopdb`, and the catalogue is `shopdb`'s.
- `LOG_LEVEL`, read from the environment by both repositories, is a candidate (E4): the
  same name in two members is a lead, and neither declares it for the solution.
- The catalogue gives tables, a view with the table it reads, columns with their keys, a
  routine with the tables it writes, roles, role membership and table grants.

The commits:

| Commit | Change | What changes across members |
|---|---|---|
| `01-service-serves-customers` | The service serves `GET /api/customers` | The front end's call to it links exactly and is no longer unmatched |
| `02-service-maps-customers` | The service maps the `customers` table | It links to the catalogue's `customers` table and columns |
| `03-web-drops-log-level` | The front end stops reading `LOG_LEVEL` | The configuration lead between the members goes |

`tests/test_fixtures.py` also compiles the whole tree as one repository, as it does every
fixture; the golden's `granularity` and `absent_entities` hold for that compile too.

Nothing here is real: names and values are made up.
