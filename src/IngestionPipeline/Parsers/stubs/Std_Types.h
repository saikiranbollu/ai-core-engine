/* Minimal AUTOSAR Std_Types stub for clang static analysis */
#ifndef STD_TYPES_H
#define STD_TYPES_H

typedef unsigned char        uint8;
typedef unsigned short       uint16;
typedef unsigned int         uint32;
typedef unsigned long long   uint64;
typedef signed char          sint8;
typedef signed short         sint16;
typedef signed int           sint32;
typedef signed long long     sint64;
typedef float                float32;
typedef double               float64;
typedef unsigned char        boolean;

typedef uint8 Std_ReturnType;
typedef struct { uint16 vendorID; uint16 moduleID; uint8 sw_major_version; uint8 sw_minor_version; uint8 sw_patch_version; } Std_VersionInfoType;

#define E_OK        ((Std_ReturnType)0)
#define E_NOT_OK    ((Std_ReturnType)1)
#define STD_HIGH    0x01u
#define STD_LOW     0x00u
#define STD_ON      0x01u
#define STD_OFF     0x00u
#define TRUE        1u
#define FALSE       0u
#define NULL_PTR    ((void*)0)

#endif /* STD_TYPES_H */
