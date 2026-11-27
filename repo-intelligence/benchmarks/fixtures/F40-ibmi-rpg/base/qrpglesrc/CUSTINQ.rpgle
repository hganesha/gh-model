     HDFTACTGRP(*NO) ACTGRP('CUSTAPP')
      * Customer inquiry: show a customer by number
     FCUSTMAST  IF   E           K DISK
     FCUSTDSP   CF   E             WORKSTN
     DpCust            S              7P0
     C     *ENTRY        PLIST
     C                   PARM                    pCust
     C                   EXSR      Init
     C                   DOW       NOT *IN03
     C                   EXFMT     CUSTINQ1
     C                   IF        *IN03
     C                   LEAVE
     C                   ENDIF
     C     CUSNO         CHAIN     CUSTMAST                           90
     C                   IF        %FOUND(CUSTMAST)
     C                   EXFMT     CUSTINQ2
     C                   ELSE
     C                   EVAL      MSG = 'Customer not found'
     C                   ENDIF
     C                   ENDDO
     C                   EVAL      *INLR = *ON
     C     Init          BEGSR
     C                   EVAL      CUSNO = pCust
     C                   CLEAR                   MSG
     C                   ENDSR
