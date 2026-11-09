"""A small two-pass 6502 assembler: asm(source, org) gives the bytes and the labels.
From the Pi1MHz fork (src/scripts/gen_ramprobe_agent.py), where it builds the probe's own code.
Labels end in a colon; TEXT and BYTES put in data; no zero-page modes but (zp),Y."""
import re

IMP = {'RTS': 0x60, 'RTI': 0x40, 'NOP': 0xEA, 'TSX': 0xBA, 'TXS': 0x9A, 'TXA': 0x8A, 'TAX': 0xAA,
       'INX': 0xE8, 'DEX': 0xCA, 'INY': 0xC8, 'DEY': 0x88, 'SEI': 0x78, 'CLI': 0x58, 'SEC': 0x38,
       'CLC': 0x18, 'CLV': 0xB8, 'PHP': 0x08, 'PLP': 0x28, 'PHA': 0x48, 'PLA': 0x68, 'TYA': 0x98,
       'TAY': 0xA8}
ACC = {'ASL': 0x0A, 'LSR': 0x4A, 'ROL': 0x2A, 'ROR': 0x6A}
IMM = {'LDA': 0xA9, 'LDX': 0xA2, 'LDY': 0xA0, 'CMP': 0xC9, 'CPX': 0xE0, 'CPY': 0xC0, 'ORA': 0x09,
       'AND': 0x29, 'EOR': 0x49, 'ADC': 0x69, 'SBC': 0xE9}
ABS = {'LDA': 0xAD, 'LDX': 0xAE, 'LDY': 0xAC, 'STA': 0x8D, 'STX': 0x8E, 'STY': 0x8C, 'JMP': 0x4C,
       'JSR': 0x20, 'CMP': 0xCD, 'CPX': 0xEC, 'CPY': 0xCC, 'INC': 0xEE, 'DEC': 0xCE, 'BIT': 0x2C,
       'ORA': 0x0D, 'AND': 0x2D, 'EOR': 0x4D, 'ADC': 0x6D, 'SBC': 0xED, 'ASL': 0x0E, 'LSR': 0x4E}
ABX = {'LDA': 0xBD, 'STA': 0x9D, 'CMP': 0xDD, 'ORA': 0x1D, 'AND': 0x3D, 'INC': 0xFE, 'LDY': 0xBC}
ABY = {'LDA': 0xB9, 'STA': 0x99, 'LDX': 0xBE, 'CMP': 0xD9}
INY = {'LDA': 0xB1, 'STA': 0x91, 'CMP': 0xD1, 'ORA': 0x11, 'AND': 0x31}
IND = {'JMP': 0x6C}
ZPG = {'LDA': 0xA5, 'STA': 0x85, 'LDX': 0xA6, 'STX': 0x86, 'LDY': 0xA4, 'STY': 0x84, 'INC': 0xE6,
       'DEC': 0xC6, 'ADC': 0x65, 'SBC': 0xE5, 'CMP': 0xC5, 'AND': 0x25, 'ORA': 0x05, 'EOR': 0x45,
       'BIT': 0x24}        # zero page: only for a literal address (&70, 112), never a label
REL = {'BEQ': 0xF0, 'BNE': 0xD0, 'BPL': 0x10, 'BMI': 0x30, 'BCC': 0x90, 'BCS': 0xB0, 'BVC': 0x50,
       'BVS': 0x70}


def num(tok, labels, final):
    """A number, a label, or those added and subtracted."""
    total = 0
    for sign, t in re.findall(r'([+-]?)\s*([&\w]+)', tok.strip()):
        if t in labels:
            v = labels[t]
        elif t.startswith('&'):
            v = int(t[1:], 16)
        elif t.isdigit():
            v = int(t)
        elif final:
            raise KeyError(t)
        else:
            v = 0
        total += -v if sign == '-' else v
    return total & 0xFFFF


def asm(src, org, const=None):
    labels = dict(const or {})
    out = b''
    for final in (False, True):
        pc, out = org, bytearray()
        for raw in src.strip().splitlines():
            line = raw.split(';')[0].strip()
            while True:
                m = re.match(r'(\w+):\s*(.*)', line)
                if not m:
                    break
                labels[m.group(1)] = pc
                line = m.group(2)
            if not line:
                continue
            parts = line.split(None, 1)
            op, arg = parts[0].upper(), (parts[1].strip() if len(parts) > 1 else '')
            if op == 'BYTES':
                b = bytes(int(x, 16) for x in arg.split())
            elif op == 'TEXT':
                b = arg.replace('\\r', '\r').encode('latin-1')
            elif op in IMP and not arg:
                b = bytes([IMP[op]])
            elif op in ACC and arg == 'A':
                b = bytes([ACC[op]])
            elif re.fullmatch(r'\(.*\),Y', arg, re.I):
                v = num(arg[1:-3], labels, final)
                if v > 0xFF:
                    raise ValueError(f'not zero page: {line}')
                b = bytes([INY[op], v])
            elif arg.startswith('('):
                v = num(arg[1:-1], labels, final)
                b = bytes([IND[op], v & 0xFF, v >> 8])
            elif arg.startswith('#'):
                b = bytes([IMM[op], num(arg[1:], labels, final) & 0xFF])
            elif op in REL:
                d = num(arg, labels, final) - (pc + 2) if (final or arg in labels) else 0
                if final and not -128 <= d <= 127:
                    raise ValueError(f'branch range: {line}')
                b = bytes([REL[op], d & 0xFF])
            elif arg.upper().endswith(',X'):
                v = num(arg[:-2], labels, final)
                b = bytes([ABX[op], v & 0xFF, v >> 8])
            elif arg.upper().endswith(',Y'):
                v = num(arg[:-2], labels, final)
                b = bytes([ABY[op], v & 0xFF, v >> 8])
            elif op in ZPG and re.fullmatch(r'&[0-9A-Fa-f]{1,2}|\d{1,3}', arg) and num(arg, labels, final) <= 0xFF:
                b = bytes([ZPG[op], num(arg, labels, final)])
            else:
                v = num(arg, labels, final)
                b = bytes([ABS[op], v & 0xFF, v >> 8])
            out += b
            pc += len(b)
    return bytes(out), labels


# Where the report lives: JIM &BF0000, through the services port.
REPORT_HI = 0xBF
HALTED_AT = 7
CALL_AT = 8
INSTALLED_AT = 13
INSTALLED = 0x5A
CUR = 0x0D7F          # the helper page showing: &FF when none of ours
AGENT_PAGE = 22

# The NMI handler at &0D00. &0D70-&0D7D is its workspace.
STUB = f"""
    STA &0D70            ; A, X, Y
    STX &0D71
    STY &0D72
    TSX
    LDA &0101,X          ; the frame: P, PC low, PC high
    STA &0D73
    LDA &0102,X
    STA &0D74
    LDA &0103,X
    STA &0D75
    STX &0D76            ; S with the frame on the stack
    LDX #{AGENT_PAGE}
    STX &FC88            ; ask for the agent's page...
ready:
    LDA &FDFF            ; ...and wait until the Pi has served it, however long that took
    CMP #{AGENT_PAGE}
    BNE ready
    JMP &FD00
resume:
    LDX &0D7F            ; the page it found: the program's own JIM page, or an agent page
    STX &FC88
    CPX #&FF
    BEQ gone
back:
    CPX &FDFF            ; the page's number is its last byte
    BNE back
gone:
    LDY &0D72
    LDX &0D71
    LDA &0D70
    RTI
"""
stub, stub_labels = asm(STUB, 0x0D00)
if len(stub) > 0x70:
    raise ValueError(f'stub is {len(stub)} bytes')

# Helper 21, typed once after switching on: X%=21:CALL &FC88. Copies the stub, says so, and
# makes Break type that line again (*KEY10), so every soft Break puts the stub back by itself.
# Its OS calls are safe: this is the foreground, called from BASIC, never from a stop.
INSTALL = f"""
    LDX #STUBLEN
copy:
    LDA stubdata-1,X
    STA &0CFF,X
    DEX
    BNE copy
    LDA #{INSTALLED_AT}
    STA &FCA6
    LDA #0
    STA &FCA7
    LDA #{REPORT_HI}
    STA &FCA8
    LDA #{INSTALLED}
    STA &FCA9
    LDA #&FF
    STA &0D7F            ; no agent page showing
    LDX #keyline         ; *KEY10: the page is &FD, and stays shown while the OS reads it
    LDY #&FD
    JSR &FFF7
    LDX #0
say:
    LDA ready,X
    BEQ home
    JSR &FFE3
    INX
    BNE say
home:
    LDX #&FF
    JMP &FC88
keyline:
    TEXT KEY10 X%=21:CALL &FC88|M\\r
ready:
    TEXT Pi1MHz probe on\\r
    BYTES 00
stubdata:
"""
install, install_labels = asm(INSTALL, 0xFD00, {'STUBLEN': len(stub)})
page_install = (install + stub).ljust(255, bytes([0])) + bytes([21])
if len(page_install) > 256:
    raise ValueError('install page')

# Helper 22, the agent, running from the Pi's memory. &FD00-&FD9F its code, &FDA0-&FDEF code the
# Pi writes for each call, &FDF0 the command (1 continue, 2 call &FDA0), &FDF1 a sequence number,
# &FDF3-&FDF5 A, X and Y for the call, &FDFF the signature the stub waits for. A command is
# acted on when &FDF1 changes, so there's no clearing to race with.
AGENT = f"""
    LDA &FCA6            ; the program's services port pointer, for afterwards
    STA &0D77
    LDA &FCA7
    STA &0D78
    LDA &FCA8
    STA &0D79
    LDA #0
    STA &0D7E            ; no command seen yet: the Pi's sequence numbers aren't 0
    JSR port
    LDX #0
report:
    LDA &0D70,X
    STA &FCA9
    INX
    CPX #{HALTED_AT}
    BNE report
    LDA #1
    STA &FCA9            ; +{HALTED_AT}: halted
wait:
    LDA &FDF1            ; a new sequence number is a new command; the Pi writes it last
    CMP &0D7E
    BEQ wait
    STA &0D7E
    LDA &FDF0
    CMP #1
    BNE call
    LDA #{HALTED_AT}
    JSR port
    LDA #0
    STA &FCA9            ; not halted
    LDA &0D77            ; the program's pointer back
    STA &FCA6
    LDA &0D78
    STA &FCA7
    LDA &0D79
    STA &FCA8
    JMP RESUME
call:
    LDA &FDF3
    LDX &FDF4
    LDY &FDF5
    JSR &FDA0
    PHP
    STA &0D7A
    STX &0D7B
    STY &0D7C
    PLA
    STA &0D7D
    LDA #{CALL_AT}
    JSR port
    LDX #0
result:
    LDA &0D7A,X
    STA &FCA9
    INX
    CPX #4
    BNE result
    LDA &0D7E
    STA &FCA9            ; the sequence number, last: the Pi waits for it
    JMP wait
port:
    STA &FCA6            ; the services port at the report, plus A
    LDA #0
    STA &FCA7
    LDA #{REPORT_HI}
    STA &FCA8
    RTS
"""
agent, _ = asm(AGENT, 0xFD00, {'RESUME': stub_labels['resume']})
if len(agent) > 0xA0:
    raise ValueError(f'agent is {len(agent)} bytes')
page_agent = agent.ljust(255, b'\0') + bytes([AGENT_PAGE])
