; Analyst patch evidence: VM code WORD indices, not physical addresses.
; before
000E0F | 000DFCE4 | 000ef45e | beq word_0x001C6E
; after
000E0F | 000DFCE4 | 000ee05e | bra word_0x001C6E
