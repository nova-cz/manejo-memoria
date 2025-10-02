from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import math
import uvicorn
import re
from typing import List

app = FastAPI(title="Virtual->Physical Translator")

# --- Permitir CORS (desarrollo) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Helpers: parse units & file
# -------------------------
UNIT_MAP = {
    'B': 1,
    'BYTE': 1, # <--- Se agrega esta línea para reconocer "BYTE" sin prefijo
    'KBYTE': 1024,
    'MBYTE': 1024**2,
    'GBYTE': 1024**3
}

def parse_size(s: str) -> int:
    if s is None: raise ValueError("Empty size")
    s = s.strip().upper().replace(" ", "")
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
# Parser del txt (SOLO memoria y tabla)
# -------------------------
def parse_txt(content: str):
    lines = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    params = {}
    page_table = {}
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
        else:
            if mode == "tabla":
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) >= 2:
                    try:
                        idx = int(parts[0])
                        frame_str = parts[1].upper()
                        page_table[idx] = None if frame_str in ('X', '') else int(frame_str)
                    except (ValueError, IndexError):
                        raise ValueError(f"Formato de la tabla de páginas inválido en línea: {ln}")

    # Validar parámetros obligatorios
    for k in ('memoria_fisica','memoria_virtual','tamanio_pagina'):
        if k not in params:
            raise ValueError(f"Falta parámetro en el archivo: {k}")

    mem_phys = parse_size(params['memoria_fisica'])
    mem_virt = parse_size(params['memoria_virtual'])
    page_size = parse_size(params['tamanio_pagina'])

    return {
        'memoria_fisica_bytes': mem_phys,
        'memoria_virtual_bytes': mem_virt,
        'tamanio_pagina_bytes': page_size,
        'page_table': page_table
    }

# -------------------------
# Cálculos
# -------------------------
def compute_bits(mem_virt, mem_phys, page_size):
    if not is_power_of_two(page_size):
        raise ValueError("El tamaño de página debe ser potencia de 2.")
    
    offset_bits = int(math.log2(page_size))
    
    if mem_virt <= 0: raise ValueError("Memoria virtual debe ser mayor a 0.")
    virtual_address_bits = int(math.log2(mem_virt))
    
    page_number_bits = virtual_address_bits - offset_bits
    
    num_pages_virtual = mem_virt // page_size
    num_frames = mem_phys // page_size
    
    if num_frames > 0:
        frame_bits = int(math.log2(num_frames)) if num_frames > 1 else 0
        
    physical_address_bits = frame_bits + offset_bits

    return {
        'offset_bits': offset_bits,
        'virtual_address_bits': virtual_address_bits,
        'page_number_bits': page_number_bits,
        'num_pages_virtual': num_pages_virtual,
        'num_frames': num_frames,
        'frame_bits': frame_bits,
        'physical_address_bits': physical_address_bits
    }

# -------------------------
# Traducción
# -------------------------
def normalize_hex(h: str) -> str:
    h = h.strip()
    if h.startswith("0x") or h.startswith("0X"):
        return h[2:]
    return h

def hex_to_bin_fixed(hex_str: str, width: int) -> str:
    val = int(hex_str, 16)
    return bin(val)[2:].zfill(width)

def translate_one(virtual_hex: str, computed: dict, page_table: dict):
    vhex = normalize_hex(virtual_hex)
    vabits = computed['virtual_address_bits']
    
    try:
        virtual_addr = int(vhex, 16)
        if virtual_addr >= (1 << vabits):
            raise ValueError(f"Dirección excede memoria virtual.")
        
        vbin = hex_to_bin_fixed(vhex, vabits)
    except Exception:
        raise ValueError(f"Dirección virtual inválida: {virtual_hex}")

    page_bits = computed['page_number_bits']
    offset_bits = computed['offset_bits']
    frame_bits = computed['frame_bits']
    
    if page_bits < 0:
        raise ValueError("Tamaño de página es mayor o igual que la memoria virtual.")

    page_bin = vbin[:page_bits]
    offset_bin = vbin[page_bits:]
    page_index = int(page_bin, 2)
    offset = int(offset_bin, 2)

    frame = page_table.get(page_index, None)
    if frame is None:
        return {
            'virtual_hex': f"0x{virtual_addr:X}",
            'virtual_bin': vbin,
            'page_index': page_index,
            'offset_bin': offset_bin,
            'page_fault': True,
            'message': f"Page fault: índice de página {page_index} inválido (No mapeado)"
        }

    if frame >= computed['num_frames']:
        return {
            'virtual_hex': f"0x{virtual_addr:X}",
            'virtual_bin': vbin,
            'page_index': page_index,
            'offset_bin': offset_bin,
            'page_fault': True,
            'message': f"Frame {frame} excede los marcos físicos ({computed['num_frames']})"
        }

    frame_bin = bin(frame)[2:].zfill(frame_bits)
    physical_bin = frame_bin + offset_bin
    physical_addr = int(physical_bin, 2)

    return {
        "virtual_hex": f"0x{virtual_addr:X}",
        "physical_hex": f"0x{physical_addr:X}",
        "page_index": page_index,
        "page_bin": page_bin,
        "frame": frame,
        "frame_bin": frame_bin,
        "offset_bin": offset_bin,
        "physical_bin": physical_bin
    }

# -------------------------
# Endpoint
# -------------------------
@app.post("/upload")
async def upload(file: UploadFile = File(...), addrs: List[str] = Form(...)):
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

    page_table = {i: parsed['page_table'].get(i, None) for i in range(computed['num_pages_virtual'])}

    results = []
    for v in addrs:
        try:
            res = translate_one(v, computed, page_table)
        except Exception as e:
            res = {'error': str(e), 'virtual_address': v}
        results.append(res)

    return JSONResponse({
        'summary': {
            'memoria_fisica_bytes': parsed['memoria_fisica_bytes'],
            'memoria_virtual_bytes': parsed['memoria_virtual_bytes'],
            'tamanio_pagina_bytes': parsed['tamanio_pagina_bytes'],
            **computed
        },
        'page_table': page_table,
        'results': results
    })

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8002, reload=True)
