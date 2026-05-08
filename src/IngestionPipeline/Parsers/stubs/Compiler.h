/* Minimal Compiler.h stub */
#ifndef COMPILER_H
#define COMPILER_H
#define AUTOMATIC
#define TYPEDEF
#define STATIC  static
#define FUNC(rettype, memclass) rettype
#define P2VAR(ptrtype, memclass, ptrclass) ptrtype *
#define P2CONST(ptrtype, memclass, ptrclass) const ptrtype *
#define CONSTP2VAR(ptrtype, memclass, ptrclass) ptrtype * const
#define CONSTP2CONST(ptrtype, memclass, ptrclass) const ptrtype * const
#define P2FUNC(rettype, ptrclass, fctname) rettype (*fctname)
#define CONST(type, memclass) const type
#define VAR(type, memclass) type
#endif
