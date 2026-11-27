# F39: CICS maps and an IMS application

A customer desk on the mainframe, kept as source members:

- `cics/maps/CUSTSET.bms`: a BMS mapset with two maps, a customer inquiry (`CUSTMAP`) and an
  order list (`ORDLST`); its `TYPE=&SYSPARM` is set when the map is assembled.
- `ims/dbd/CUSTDB.dbd`: a HIDAM database with a three-level hierarchy (customer, order,
  order line) and its primary index.
- `ims/psb/CUSTPGM.psb`: the PSB of program `CUSTPGM`: an alternate PCB to the `ORDNOTE`
  transaction, a PCB that reads customers and updates orders and order lines, and one that
  reads customers through the index.
- `ims/mfs/CUSTF.mfs`: the 3270 format of the inquiry, its input message (which starts with
  the transaction code) and its output message.
- `cobol/CUSTPGM.cbl`: the program the PSB is named for.
- `.rkp/inputs/ims/stage1.gen`: the stage-1 system definition: the applications and their
  transactions. `ORDPGM`, whose transaction the program sends notes to, is not in the
  repository.
