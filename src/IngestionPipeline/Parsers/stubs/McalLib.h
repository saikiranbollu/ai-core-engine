/* Stub: McalLib.h / McalUtil macros for clang static analysis.
   These expand to simple expressions so clang can resolve types. */
#ifndef MCALLIB_H
#define MCALLIB_H

#include "Std_Types.h"
#include "Mcal_ExecutionContext.h"

/* SFR read/write — expand to assignments so clang can see the
   MEMBER_REF_EXPR for type resolution. */
#define MCALUTIL_SFRWRITE(reg, val)    ((reg) = (val))
#define MCALUTIL_SFRREAD(result, reg)  ((result) = (reg))

/* Bit manipulation helpers */
#define MCALUTIL_EXTRACTBITS_VCC(val, pos, width)  (((val) >> (pos)) & ((1u << (width)) - 1u))
#define MCALUTIL_EXTRACTBITS_VVC(val, pos, width)  (((val) >> (pos)) & ((1u << (width)) - 1u))
#define MCALUTIL_INSERTBITS_VCCC(val, pos, width, ins) \
    (((val) & ~(((1u << (width)) - 1u) << (pos))) | (((ins) & ((1u << (width)) - 1u)) << (pos)))
#define MCALUTIL_INSERTBITS_VVVC(val, pos, width, ins) \
    (((val) & ~(((1u << (width)) - 1u) << (pos))) | (((ins) & ((1u << (width)) - 1u)) << (pos)))

/* Misc utilities */
#define MCALUTIL_GETCOREID()              ((uint32)0u)
#define MCALUTIL_COUNTLEADINGZEROS(val)   ((uint32)0u)
#define MCALUTIL_SWPMSK(addr, clr, set)    (*(addr) = ((*(addr)) & (clr)) | (set))
#define MCALUTIL_UNUSEDPARAM(x)           ((void)(x))

/* IFX_INLINE / LOCAL_INLINE */
#ifndef IFX_INLINE
#define IFX_INLINE static inline
#endif
#ifndef LOCAL_INLINE
#define LOCAL_INLINE static inline
#endif

#endif /* MCALLIB_H */
