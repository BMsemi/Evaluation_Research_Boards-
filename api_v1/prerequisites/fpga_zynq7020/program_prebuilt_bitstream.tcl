if {[llength $argv] < 1} {
    puts "ERROR: usage: vivado -mode batch -source program_prebuilt_bitstream.tcl -tclargs <bitstream.bit>"
    exit 1
}

set bit_file [file normalize [lindex $argv 0]]
if {![file exists $bit_file]} {
    puts "ERROR: bitstream not found: $bit_file"
    exit 1
}

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
