# Personnel library (Natural, Adabas and IDMS)

A Natural library exported as source files, with the Adabas DDMs it reads and the IDMS
schema of the payroll batch beside it:

- `natural/EMPMAINT.nsp`: a structured-mode program. It shows the map `EMPMAP`, reads
  `EMPLOYEES` through a view and updates the salary of the record it read, finds the
  employee's vehicles in `VEHICLES`, calls the subprogram `EMPSAL` (CALLNAT), an inline
  subroutine (PERFORM), an assembler program (CALL) and a program named in a variable
  (FETCH RETURN). Its local data has an initial value that looks like a key: planted test
  data that must never be copied.
- `natural/EMPSAL.nsn`: a subprogram with parameter data. It finds and gets an employee,
  updates it and writes an audit row with Natural SQL.
- `natural/EMPMAP.nsm`: the map, with its fields at their positions and a help routine.
- `natural/EMPLDA.nsl`: the local data area `EMPMAINT` uses, with a name used in two groups.
- `natural/EMPLOYEES.nsd`, `natural/VEHICLES.nsd`: Adabas DDMs, with a group, a
  multiple-value field, a periodic group and descriptors.
- `natural/VEHRPT.nsp`: a reporting-mode report with editor line numbers, reading the DDM
  by its own name.
- `.rkp/inputs/idms/empschm.schema`: an IDMS schema with two records, an 88-level
  condition and a set. `.rkp/inputs/idms/empss01.txt`: a subschema of it.
