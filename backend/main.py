# main.py
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import math
import uvicorn
import re

app = FastAPI(title="Virtual->Physical Translator")



# --- Permitir CORS (desarrollo) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],    # para desarrollo: permite cualquier origen
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Helpers: parse units & file
# -------------------------
UNIT_MAP = {
    'B': 1,
    'KBYTE': 1024,
    'MBYTE': 1024**2,
    'GBYTE': 1024**3
}

def parse_size(s: str) -> int:
    # acepta formatos como "8 KByte", "1 GByte", case-insensitive
    if s is None: raise ValueError("Empty size")
    s = s.strip().upper().replace(" ", "")
    # extraer numero y unidad (puede venir sin unidad)
    m = re.match(r"^(\d+)([KMGB]?BYTE|B)?$", s)
    if not m:
        raise ValueError(f"Formato de tamaño inválido: {s}")
    num = int(m.group(1))
    unit = (m.group(2) or "B").upper()
    if unit not in UNIT_MAP:
        raise ValueError(f"Unidad desconocida: {unit}")
    return num * UNIT_MAP[unit]

def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n-1)) == 0

# -------------------------
# Parser del txt (formato simple)
# -------------------------
def parse_txt(content: str):
    # Devuelve dict con memoria_fisica_bytes, memoria_virtual_bytes, tamanio_pagina_bytes, page_table(dict), virtual_addrs(list)
    lines = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    params = {}
    page_table = {}
    virtual_addrs = []
    mode = None

    for ln in lines:
        low = ln.lower()
        if low.startswith("memoria_fisica"):
            _, val = ln.split(":",1); params['memoria_fisica'] = val.strip()
        elif low.startswith("memoria_virtual"):
            _, val = ln.split(":",1); params['memoria_virtual'] = val.strip()
        elif low.startswith("tamanio_pagina") or low.startswith("tamaño_pagina"):
            _, val = ln.split(":",1); params['tamanio_pagina'] = val.strip()
        elif low.startswith("tabla_paginas:") or low.startswith("tabla_de_paginas:"):
            mode = "tabla"
        elif low.startswith("direcciones_virtuales:") or low.startswith("direcciones:"):
            mode = "dirs"
        else:
            if mode == "tabla":
                # línea tipo: index, frame, (control opc)
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) >= 2:
                    idx = int(parts[0])
                    frame = parts[1].upper()
                    if frame == 'X' or frame == '':
                        page_table[idx] = None
                    else:
                        page_table[idx] = int(frame)
            elif mode == "dirs":
                # aceptar con o sin 0x
                hx = ln.strip()
                virtual_addrs.append(hx)
            else:
                # intentar reconocer líneas 'index,frame' sin encabezado
                if "," in ln:
                    parts = [p.strip() for p in ln.split(",")]
                    if parts[0].isdigit():
                        idx = int(parts[0])
                        frame = parts[1].upper()
                        page_table[idx] = None if frame in ('X','') else int(frame)
                else:
                    # fallback: si parece hex (0x or hex digits) considerarlo una dir virtual
                    if re.match(r'^(0x)?[0-9A-Fa-f]+$', ln):
                        virtual_addrs.append(ln)
    # Validar parámetros obligatorios
    for k in ('memoria_fisica','memoria_virtual','tamanio_pagina'):
        if k not in params:
            raise ValueError(f"Falta parámetro en el archivo: {k}")
    # convertir unidades
    mem_phys = parse_size(params['memoria_fisica'])
    mem_virt = parse_size(params['memoria_virtual'])
    page_size = parse_size(params['tamanio_pagina'])
    return {
        'memoria_fisica_bytes': mem_phys,
        'memoria_virtual_bytes': mem_virt,
        'tamanio_pagina_bytes': page_size,
        'page_table': page_table,
        'virtual_addrs': virtual_addrs
    }

# -------------------------
# Cálculos (bits, etc.)
# -------------------------
def compute_bits(mem_virt, mem_phys, page_size):
    if not is_power_of_two(page_size):
        raise ValueError("El tamaño de página debe ser potencia de 2.")
    offset_bits = int(round(math.log2(page_size)))
    virtual_address_bits = int(round(math.log2(mem_virt)))
    page_number_bits = virtual_address_bits - offset_bits
    num_pages_virtual = mem_virt // page_size
    num_frames = mem_phys // page_size
    frame_bits = int(math.ceil(math.log2(num_frames))) if num_frames>1 else 1
    return {
        'offset_bits': offset_bits,
        'virtual_address_bits': virtual_address_bits,
        'page_number_bits': page_number_bits,
        'num_pages_virtual': num_pages_virtual,
        'num_frames': num_frames,
        'frame_bits': frame_bits
    }

# -------------------------
# Traducción por dirección
# -------------------------
def normalize_hex(h: str) -> str:
    h = h.strip()
    if h.startswith("0x") or h.startswith("0X"):
        return h[2:]
    return h

def hex_to_bin_fixed(hex_str: str, width: int) -> str:
    val = int(hex_str, 16)
    b = bin(val)[2:].zfill(width)
    return b

def translate_one(virtual_hex: str, computed: dict, page_table: dict):
    # Devuelve dict con pasos y resultado o page fault
    vhex = normalize_hex(virtual_hex)
    vabits = computed['virtual_address_bits']
    try:
        vbin = hex_to_bin_fixed(vhex, vabits)
    except Exception as e:
        raise ValueError(f"Dirección virtual inválida: {virtual_hex}") from e

    page_bits = computed['page_number_bits']
    offset_bits = computed['offset_bits']
    frame_bits = computed['frame_bits']

    page_bin = vbin[:page_bits]
    offset_bin = vbin[page_bits:]
    page_index = int(page_bin,2)
    # Validaciones
    if page_index >= computed['num_pages_virtual']:
        return {'error': f"Dirección excede memoria virtual: page_index={page_index}"}
    # buscar en tabla
    frame = page_table.get(page_index, None)
    if frame is None:
        return {
            'virtual_hex': "0x"+vhex.upper(),
            'virtual_bin': vbin,
            'page_index': page_index,
            'page_bin': page_bin,
            'offset_bin': offset_bin,
            'page_fault': True,
            'message': f"Page fault: entrada inválida (índice {page_index})"
        }
    # validar marco
    if frame >= computed['num_frames']:
        return {'error': f"Frame number {frame} exceeds physical frames {computed['num_frames']}"}
    frame_bin = bin(frame)[2:].zfill(frame_bits)
    physical_bin = frame_bin + offset_bin

    # pad left to multiple of 4 for hex conversion
    pad = (4 - (len(physical_bin) % 4)) % 4
    physical_bin_padded = ("0"*pad) + physical_bin
    physical_hex = hex(int(physical_bin_padded,2))[2:].upper()
    physical_hex = "0x" + physical_hex

    return {
        'virtual_hex': "0x"+vhex.upper(),
        'virtual_bin': vbin,
        'page_index': page_index,
        'page_bin': page_bin,
        'offset_bin': offset_bin,
        'frame': frame,
        'frame_bin': frame_bin,
        'physical_bin': physical_bin,
        'physical_hex': physical_hex,
        'page_fault': False
    }

# -------------------------
# Endpoint
# -------------------------
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename.endswith(".txt"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .txt")
    content = (await file.read()).decode('utf-8', errors='ignore')
    try:
        parsed = parse_txt(content)
        computed = compute_bits(parsed['memoria_virtual_bytes'],
                                parsed['memoria_fisica_bytes'],
                                parsed['tamanio_pagina_bytes'])
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # preparar page_table: asegurar que existan todas las entradas hasta num_pages_virtual
    page_table = {}
    for i in range(computed['num_pages_virtual']):
        page_table[i] = parsed['page_table'].get(i, None)

    results = []
    for v in parsed['virtual_addrs']:
        try:
            res = translate_one(v, computed, page_table)
        except Exception as e:
            res = {'error': str(e), 'virtual': v}
        results.append(res)

    return JSONResponse({
        'summary': {
            'memoria_fisica_bytes': parsed['memoria_fisica_bytes'],
            'memoria_virtual_bytes': parsed['memoria_virtual_bytes'],
            'tamanio_pagina_bytes': parsed['tamanio_pagina_bytes'],
            **computed
        },
        'page_table': {k: page_table[k] for k in sorted(page_table.keys())},
        'results': results
    })

# Para desarrollo local opcional
if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8002, reload=True)
