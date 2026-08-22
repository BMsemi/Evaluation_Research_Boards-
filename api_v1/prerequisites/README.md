# Prerequisites

This folder contains the source files needed to recreate the current scan-debug hardware setup.

## Folders

- `fpga_zynq7020/`: AX7020/Zynq-7020 RTL, constraints, and Vivado programming TCL.
- `teensy_dac_adc/`: DAC/ADC Teensy firmware that accepts `SCAN_CUSTOM_RAILS <vcc_set_mV> <vcc_wl_set_mV>`.
- `saleae_ubuntu/`: Logic 2 automation capture helper used on the Ubuntu Saleae host.

## Deployment Targets

Copy `fpga_zynq7020/*` to:

```text
C:/Users/geethika/zynq_scan_debug
```

Copy `saleae_ubuntu/run_fpga_scan0000_la12_15_capture.py` to:

```text
/home/ubuntu-24-04/saleae-api
```

Flash `teensy_dac_adc/DAC_analog_vltgs.ino` to the DAC/ADC Teensy with the other files kept beside it in the same sketch folder.

## Important Defaults

- Read verify rails: `Vcc_set=1.2 V`, `Vcc_wl_set=2.5 V`.
- Set ramp default: `Vcc_set=2.0,2.3,2.4,2.5,2.8 V`; `Vcc_wl_set=0.5..2.0 V`.
- Reset ramp default: `Vcc_set=2.0,2.3,2.6,2.9,3.2,3.5 V`; `Vcc_wl_set=0.5,0.8,1.1,1.4,1.7,2.0,2.5 V`.
- Shunt: `470 ohms`, override with `--shunt-ohms`.
- `SCAN_CUSTOM_RAILS` drives only `Vcc_set` and `Vcc_wl_set`; read/reset rails are held at `0 V` by the included firmware for this scan-debug API flow.
