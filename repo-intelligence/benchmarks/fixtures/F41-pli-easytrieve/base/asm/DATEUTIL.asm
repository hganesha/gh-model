*---------------------------------------------------------------------*
* DATEUTIL: return the business date from the DATEIN control file.   *
*---------------------------------------------------------------------*
DATEUTIL CSECT
DATEUTIL AMODE 31
DATEUTIL RMODE ANY
         ENTRY DATENEXT
         SAVE  (14,12),,DATEUTIL save the caller's registers
         LR    12,15
         USING DATEUTIL,12
         GETMAIN R,LV=WORKLEN
         OPEN  (DATEIN,(INPUT))
         GET   DATEIN,DATEREC
         CLOSE (DATEIN)
         CLC   DATEREC(8),=CL8' ' is the date blank?
         BNE   GOTDATE
         WTO   'DATEUTIL: NO DATE IN DATEIN'
         ABEND 801,DUMP
GOTDATE  DS    0H
         LINK  EP=CALCHOL,PARAM=(DATEREC),VL=1 holiday calendar
         L     15,=V(DATEFMT)
         BALR  14,15
         FREEMAIN R,LV=WORKLEN,A=(1)
         RETURN (14,12),RC=0
DATENEXT DS    0H
         LOAD  EPLOC=MODNAME
         BR    14
DATEIN   DCB   DDNAME=DATEIN,DSORG=PS,MACRF=GM,RECFM=FB, control file  X
               LRECL=80
DATEREC  DS    CL80
MODNAME  DC    CL8'DATEFMT'
WORKLEN  EQU   256
         LTORG
         END   DATEUTIL
