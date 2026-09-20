# Gantry Driver

`GrblController` contains the hardware behavior migrated from the verified
`AutoSpinmotorSystem/hardware/xyz_stage/` implementation.

It owns GRBL serial communication, homing, alarm handling, status parsing,
software-limit enforcement, jog cancellation, emergency reset, and Z-brake
coordination. `src.hardware.gantry_backend.GantryBackend` is the L3 facade and
does not open serial ports or generate G-code.
