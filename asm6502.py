"""A small two-pass 6502 assembler: asm(source, org) gives the bytes and the labels.
From an early version of the Pi1MHz RAM probe, whose own code is now beebasm's.
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
       'ORA': 0x0D, 'AND': 0x2D, 'EOR': 0x4D, 'ADC': 0x6D, 'SBC': 0xED, 'ASL': 0x0E, 'LSR': 0x4E,
       'ROR': 0x6E}
ABX = {'LDA': 0xBD, 'STA': 0x9D, 'CMP': 0xDD, 'ORA': 0x1D, 'AND': 0x3D, 'INC': 0xFE, 'LDY': 0xBC}
ABY = {'LDA': 0xB9, 'STA': 0x99, 'LDX': 0xBE, 'CMP': 0xD9}
INY = {'LDA': 0xB1, 'STA': 0x91, 'CMP': 0xD1, 'ORA': 0x11, 'AND': 0x31}
IND = {'JMP': 0x6C}
ZPG = {'LDA': 0xA5, 'STA': 0x85, 'LDX': 0xA6, 'STX': 0x86, 'LDY': 0xA4, 'STY': 0x84, 'INC': 0xE6,
       'DEC': 0xC6, 'ADC': 0x65, 'SBC': 0xE5, 'CMP': 0xC5, 'AND': 0x25, 'ORA': 0x05, 'EOR': 0x45,
       'BIT': 0x24, 'ROR': 0x66}        # zero page: only for a literal address (&70, 112), never a label
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

