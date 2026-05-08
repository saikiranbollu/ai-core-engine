/* Stub: Adc_Cfg.h - Configuration (features tuned to minimize #error directives) */
#ifndef ADC_CFG_H
#define ADC_CFG_H

#include "Std_Types.h"

/* Version info — must match the checks in Adc.c */
#define ADC_AR_RELEASE_MAJOR_VERSION   4u
#define ADC_AR_RELEASE_MINOR_VERSION   6u
#define ADC_AR_RELEASE_REVISION_VERSION 0u
#define ADC_SW_MAJOR_VERSION           2u
#define ADC_SW_MINOR_VERSION           30u
#define ADC_SW_PATCH_VERSION           1u

/* Module IDs */
#define ADC_MODULE_ID   123u
#define ADC_VENDOR_ID   17u

/* Feature switches — disable cross-module checks, enable code-path analysis */
#define ADC_DEINIT_API                  STD_ON
#define ADC_DEV_ERR_CHECK               STD_ON
#define ADC_DEV_ERR_REPORTING           STD_OFF
#define ADC_DMA_RESULT_HANDLING         STD_ON
#define ADC_ENABLE_DIAGNOSTICS          STD_ON
#define ADC_ENABLE_LIMIT_CHECK          STD_ON
#define ADC_ENABLE_POST_PROCESSING      STD_ON
#define ADC_ENABLE_START_STOP_GROUP_API STD_ON
#define ADC_ERROR_EVENT_HANDLER         STD_ON
#define ADC_ERU_TRIGGER_CONFIGURATION   STD_ON
#define ADC_GRP_NOTIF_CAPABILITY        STD_ON
#define ADC_HW_TRIGGER_API              STD_ON
#define ADC_INIT_CHECK_API              STD_ON
#define ADC_PARTITION_ERR_CHECK         STD_OFF
#define ADC_PROD_ERR_REPORTING          STD_OFF
#define ADC_READ_GROUP_API              STD_ON
#define ADC_RUNTIME_ERR_REPORTING       STD_OFF
#define ADC_SAFETY_ERR_REPORTING        STD_OFF
#define ADC_TIMER_TRIGGER_CONFIGURATION STD_ON
#define ADC_VERSION_INFO_API            STD_ON

/* DEM reporting — all OFF to skip cross-module version checks */
#define ADC_E_CDSP_RESET_FAILURE_DEM_REPORTING          STD_OFF
#define ADC_E_DMA_TRANSFER_FAILURE_DEM_REPORTING        STD_OFF
#define ADC_E_REG_READBACK_FAILURE_DEM_REPORTING        STD_OFF
#define ADC_E_STATE_TRANSITION_FAILURE_DEM_REPORTING    STD_OFF

/* Dummy values for module configuration */
#define ADC_MAX_HW_UNITS  12u
#define ADC_MAX_GROUPS     64u
#define ADC_MAX_CHANNELS   16u
#define ADC_MAX_RESULT_REG 16u
#define ADC_MAX_PARTITIONS  4u

/* Error IDs */
#define ADC_GETVERSIONINFO_SID  0x00u
#define ADC_INIT_SID            0x01u
#define ADC_DEINIT_SID          0x02u
#define ADC_STARTSCAN_SID       0x03u
#define ADC_E_PARAM_POINTER     0x14u
#define ADC_E_UNINIT             0x0Au
#define ADC_E_ALREADY_INITIALIZED 0x0Du
#define ADC_REPORT_NONE          0u
#define MCAL_DEVELOPMENT_ERR     1u
#define MCAL_RUNTIME_ERR         2u
#define MCAL_SAFETY_ERR          3u

#endif /* ADC_CFG_H */
