# F23-multi-repo-solution

One application spread over two repositories and a database catalogue dump. It is
used by `tests/test_solution.py`, not by `golden_check`: a golden fixture compiles one
repository, and this fixture is several.

| Directory | What it is | How the test uses it |
|---|---|---|
| `web/` | A TypeScript front end calling the orders API with `fetch` | Copied into its own Git repository and compiled |
| `service/` | A FastAPI service serving `/api/orders` and reading the `orders` table through SQLAlchemy | Copied into its own Git repository and compiled |
| `solution/` | The solution root: `.rkp/solution.json` and the catalogue dump under `.rkp/inputs/catalog/` | Copied as the directory that holds both repositories |

What a correct load and resolution gives:

- `GET /api/orders/{id}` from `web` links to the service's endpoint exactly (E3): one
  endpoint serves that path.
- `/api/orders` from `web` is a candidate (E4) for both `GET` and `POST`: `fetch` does
  not state its method to the extractor, and two endpoints serve the path.
- `/api/customers` is unmatched: no member serves it.
- The service's `orders` table, and its columns, link exactly to the catalogue's: the
  manifest says the service's tables are in `shopdb`, and the catalogue is `shopdb`'s.
- `LOG_LEVEL`, read by both repositories, is one configuration key.
- The catalogue gives tables, a view with the table it reads, columns with their keys, a
  routine with the tables it writes, roles, role membership and table grants.

Nothing here is real: names and values are made up.
