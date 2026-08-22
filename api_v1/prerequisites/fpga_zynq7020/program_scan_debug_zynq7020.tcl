set script_dir [file dirname [file normalize [info script]]]
set bit_file [file join $script_dir "caravel_scan_debug_fpga.bit"]

open_hw
connect_hw_server
open_hw_target
set devs [get_hw_devices]
if {[llength $devs] == 0} {
    puts "ERROR: no JTAG hardware devices found"
    exit 1
}

set dev ""
foreach candidate $devs {
    set part [get_property PART $candidate]
    puts "DEVICE $candidate PART=$part"
    if {[regexp -nocase {(xc7z|7z020)} "$candidate $part"]} {
        set dev $candidate
        break
    }
}

if {$dev eq ""} {
    puts "ERROR: no programmable xc7z020 device found in JTAG chain"
    exit 1
}

current_hw_device $dev
refresh_hw_device $dev
set_property PROGRAM.FILE $bit_file $dev
program_hw_devices $dev
puts "PROGRAMMED $dev with $bit_file"
exit
