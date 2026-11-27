"""The assembler pack's vocabulary: platform terms, the constructs it counts and the tables it
counts them by.

Keys: an assembled program is `Program {platform: "mainframe", name: <first CSECT>}`
(contract 11.1 item 2), the name every `CALL`, `LINK` and `EXEC PGM=` uses; a program it
runs is the same key, not declared.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

TERMS = (
    Term("asm:Program", K.PROGRAM, "lossy", "an assembler program: its control sections, entry points, files and external references in attributes"),
)

# System macros counted by name (W8.5); every other macro is a user macro, counted together.
LISTED_MACROS = ("ABEND", "CLOSE", "FREEMAIN", "GET", "GETMAIN", "OPEN", "PUT", "READ", "RETURN", "SAVE", "STORAGE", "WRITE", "WTO")
CALL_MACROS = ("ATTACH", "CALL", "LINK", "LOAD", "XCTL")
FILE_MACROS = ("ACB", "DCB")
BMS_MACROS = ("DFHMDF", "DFHMDI", "DFHMSD")

SECTIONS = frozenset({"CSECT", "RSECT", "START"})
DIRECTIVES = frozenset("""
ACONTROL ADATA ALIAS AMODE CATTR CCW CCW0 CCW1 CEJECT CNOP COM COPY CXD DC DROP DS DSECT DXD EJECT END ENTRY EQU EXITCTL EXTRN ICTL ISEQ LOCTR LTORG MNOTE OPSYN ORG
POP PRINT PROCESS PUNCH PUSH REPRO RMODE SPACE TITLE USING WXTRN XATTR
""".split())
CONDITIONAL = frozenset("ACTR AEJECT AGO AIF ANOP AREAD ASPACE GBLA GBLB GBLC LCLA LCLB LCLC MEXIT SETA SETAF SETB SETC SETCF".split())

# Machine instructions (z/Architecture, with the extended branch and jump mnemonics). An
# operation in none of these tables is a macro.
MNEMONICS = frozenset("""
A AD ADB ADBR ADR AE AEB AEBR AER AFI AG AGF AGFI AGFR AGHI AGHIK AGR AGRK AGSI AH AHHHR AHHLR AHI AHIK AHY AIH AL ALC ALCG ALCGR ALCR ALFI ALG ALGF ALGFI
ALGFR ALGHSIK ALGR ALGRK ALGSI ALHHHR ALHHLR ALHSIK ALR ALRK ALSI ALSIH ALSIHN ALY AP AR ARK ASI AU AUR AW AWR AXBR AXR AY
B BAL BALR BAS BASR BASSM BC BCR BCT BCTG BCTGR BCTR BE BER BH BHR BL BLR BM BMR BNE BNER BNH BNHR BNL BNLR BNM BNMR BNO BNOR BNP BNPR BNZ BNZR BO BOR BP BPR
BR BRAS BRASL BRC BRCL BRCT BRCTG BRCTH BRXH BRXHG BRXLE BRXLG BSM BXH BXHG BXLE BXLEG BZ BZR
C CD CDB CDBR CDFBR CDGBR CDR CDS CDSG CDSY CE CEB CEBR CER CFC CFDBR CFEBR CFI CG CGDBR CGEBR CGF CGFI CGFR CGFRL CGH CGHI CGHRL CGHSI CGIB CGIJ CGR CGRB CGRJ
CGRL CGRT CH CHF CHHR CHHSI CHI CHLR CHRL CHSI CHY CIB CIH CIJ CIT CKSM CL CLC CLCL CLCLE CLCLU CLFHSI CLFI CLFIT CLG CLGF CLGFI CLGFR CLGFRL CLGHRL CLGHSI CLGIB
CLGIJ CLGIT CLGR CLGRB CLGRJ CLGRL CLGRT CLHF CLHHR CLHHSI CLHLR CLHRL CLI CLIB CLIH CLIJ CLIY CLM CLMH CLMY CLR CLRB CLRJ CLRL CLRT CLST CLY CP CPSDR CR CRB CRJ
CRL CRT CS CSG CSP CSST CSY CU12 CU14 CU21 CU24 CU41 CU42 CUSE CUTFU CUUTF CVB CVBG CVBY CVD CVDG CVDY CXBR CXR CY
D DD DDB DDBR DDR DE DEB DEBR DER DL DLG DLGR DLR DP DR DSG DSGF DSGFR DSGR DXBR DXR
EAR ECAG ED EDMK EPSW ETND EX EXRL
FIDBR FIDR FIEBR FIER FLOGR
HDR HER
IAC IC ICM ICMH ICMY ICY IIHF IIHH IIHL IILF IILH IILL IPM
J JAS JASL JC JCT JCTG JE JH JL JLE JLH JLL JLM JLNE JLNH JLNL JLNM JLNO JLNP JLNZ JLO JLP JLU JLZ JM JNE JNH JNL JNM JNO JNP JNZ JO JP JXH JXHG JXLE JXLEG JZ
L LA LAA LAAG LAAL LAALG LAE LAEY LAM LAMY LAN LANG LAO LAOG LARL LAT LAX LAXG LAY LB LBH LBR LCDBR LCDR LCEBR LCER LCGFR LCGR LCR LCXBR LCXR LD LDE LDEB LDEBR
LDER LDGR LDR LDXBR LDXR LDY LE LEDBR LEDR LER LEXBR LEXR LEY LFH LFHAT LG LGAT LGB LGBR LGDR LGF LGFI LGFR LGFRL LGH LGHI LGHR LGHRL LGR LGRL LH LHH LHI LHR LHRL
LHY LLC LLCH LLCR LLGC LLGCR LLGF LLGFAT LLGFR LLGFRL LLGH LLGHR LLGHRL LLGT LLGTAT LLGTR LLH LLHH LLHR LLHRL LLIHF LLIHH LLIHL LLILF LLILH LLILL LM LMD LMG LMH
LMY LNDBR LNDR LNEBR LNER LNGFR LNGR LNR LNXBR LNXR LOC LOCG LOCGR LOCR LPDBR LPDR LPEBR LPER LPGFR LPGR LPQ LPR LPXBR LPXR LR LRL LRV LRVG LRVGR LRVH LRVR LT
LTDBR LTDR LTEBR LTER LTG LTGF LTGFR LTGR LTR LTXBR LTXR LXDB LXDBR LXDR LXEB LXEBR LXR LY LZDR LZER LZXR
M MAD MADB MADBR MADR MAE MAEB MAEBR MAER MD MDB MDBR MDE MDEB MDEBR MDER MDR ME MEE MEEB MEEBR MEER MER MFY MGHI MH MHI MHY ML MLG MLGR MLR MP MR MS MSCH
MSD MSDB MSDBR MSDR MSE MSEB MSEBR MSER MSFI MSG MSGF MSGFI MSGFR MSGR MSR MSY MVC MVCDK MVCIN MVCK MVCL MVCLE MVCLU MVCOS MVCP MVCS MVCSK MVGHI MVHHI MVHI MVI
MVIY MVN MVO MVST MVZ MXBR MXD MXDB MXDBR MXDR MXR MY
N NC NG NGR NGRK NI NIHF NIHH NIHL NILF NILH NILL NIY NOP NOPR NR NRK NY
O OC OG OGR OGRK OI OIHF OIHH OIHL OILF OILH OILL OIY OR ORK OY
PACK PFD PFDRL PKA PKU PLO POPCNT PPA
RISBG RISBGN RISBHG RISBLG RLL RLLG RNSBG ROSBG RXSBG
S SAM24 SAM31 SAM64 SAR SD SDB SDBR SDR SE SEB SEBR SER SG SGF SGFR SGR SGRK SH SHHHR SHHLR SHY SL SLA SLAG SLAK SLB SLBG SLBGR SLBR SLDA SLDL SLFI SLG SLGF
SLGFI SLGFR SLGR SLGRK SLHHHR SLHHLR SLL SLLG SLLK SLR SLRK SLY SP SPM SR SRA SRAG SRAK SRDA SRDL SRK SRL SRLG SRLK SRP SRST SRSTU ST STAM STAMY STC STCH STCK
STCKE STCKF STCM STCMH STCMY STCY STD STDY STE STEY STFH STFPC STG STGRL STH STHH STHRL STHY STM STMG STMH STMY STOC STOCG STPQ STRL STRV STRVG STRVH STY SU SUR
SVC SW SWR SXBR SXR SY
TAM TBDR TBEDR TCDB TCEB TCXB THDER THDR TM TMH TMHH TMHL TML TMLH TMLL TMY TP TR TRE TRT TRTE TRTO TRTR TRTRE TROO TROT TRTT TS
UNPK UNPKA UNPKU UPT
X XC XG XGR XGRK XI XIHF XILF XIY XR XRK XY
ZAP
""".split())

_CONSTRUCTS = (
    ("csect", "a control section (CSECT, RSECT or START): the first names the Program", "entity:Program"),
    ("entry", "ENTRY: another entry point of the program", "attribute:entries"),
    ("extrn", "EXTRN, WXTRN or a V-type constant: an external reference", "attribute:externals"),
    ("dsect", "a dummy section (a record or control block layout)", "unmapped"),
    ("copy", "COPY of a member", "unmapped"),
    ("directive", "another assembler instruction (USING, DC, DS, EQU, LTORG, ORG ...)", "unmapped"),
    ("conditional_assembly", "conditional assembly (AIF, AGO, SETx, LCLx, GBLx ...)", "unmapped"),
    ("macro_definition", "a macro definition (MACRO ... MEND)", "unmapped"),
    ("machine_instruction", "a machine instruction", "unmapped"),
    ("user_macro", "a macro not counted by name: a user or other system macro", "unmapped"),
    ("call", "CALL of an external entry", "relation:CALLS"),
    ("link", "LINK EP= of a program", "relation:CALLS"),
    ("xctl", "XCTL EP= to a program", "relation:CALLS"),
    ("load", "LOAD EP= of a program", "relation:CALLS"),
    ("attach", "ATTACH EP= of a program", "relation:CALLS"),
    ("call_dynamic", "CALL, LINK, XCTL, LOAD or ATTACH of a program named at run time (a register, EPLOC= or DE=)", "unmapped"),
    ("dcb", "DCB: a sequential or partitioned file by DD name", "attribute:files"),
    ("acb", "ACB: a VSAM file by DD name", "attribute:files"),
    ("exec_cics", "EXEC CICS", "unmapped"),
    ("exec_cics_link", "EXEC CICS LINK or XCTL of a literal program", "relation:CALLS"),
    ("exec_cics_map", "EXEC CICS SEND MAP of a literal map and map set", "relation:RENDERS"),
    ("exec_sql", "EXEC SQL: an embedded SQL statement", "relation:READS_FROM"),
    ("exec_sql_dynamic", "EXEC SQL PREPARE, EXECUTE, cursor and connection statements", "unmapped"),
    ("bms_macro", "a BMS macro (DFHMSD, DFHMDI, DFHMDF): read by the bms pack", "unmapped"),
) + tuple((f"macro.{m}", f"the {m} macro", "attribute:files" if m == "OPEN" else "unmapped") for m in LISTED_MACROS)

CONSTRUCTS = tuple(sorted((Construct(f"asm:{name}", text, modelled) for name, text, modelled in _CONSTRUCTS), key=lambda c: c.id))
