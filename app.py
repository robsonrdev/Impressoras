import os
import time
import threading
import requests
import socket
from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- CONFIGURAÇÕES ATUALIZADAS ---
# Caminho da sua pasta de rede
PASTA_RAIZ = r'H:\Outros computadores\Meu laptop\Empresas\3D Super Tech\Design e Projetos 3D\2 - 3D Super Tech\Filtro para Umidificador'

# Mudamos de "192.168.10." para "192.168.1." conforme sua nova rede
IP_BASE = "192.168.1."   
RANGE_IPS = (1, 255)     

# Dicionário global para guardar as impressoras
IMPRESSORAS_ENCONTRADAS = {}

# --- FUNÇÃO 1: VERIFICAÇÃO RELÂMPAGO (SOCKET) ---
def testar_conexao_rapida(ip, porta=80):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.1) 
        resultado = sock.connect_ex((ip, porta))
        sock.close()
        return resultado == 0
    except:
        return False

# --- FUNÇÃO 2: SCANNER DETALHADO ---
def verificar_ip(i):
    ip = f"{IP_BASE}{i}"
    
    if not testar_conexao_rapida(ip):
        if ip in IMPRESSORAS_ENCONTRADAS:
            IMPRESSORAS_ENCONTRADAS[ip].update({'status': 'offline', 'cor': 'offline', 'msg': 'OFFLINE'})
        return

    url = f"http://{ip}/printer/objects/query?print_stats&display_status"
    try:
        resp = requests.get(url, timeout=2)
        if resp.status_code == 200:
            dados = resp.json()
            status = dados['result']['status']['print_stats']['state']
            filename = dados['result']['status']['print_stats']['filename']
            progresso_raw = dados['result']['status']['display_status']['progress']
            progresso = int(progresso_raw * 100)
            
            if status == "printing" and progresso >= 100: status = "complete"

            cor_status = "ready"
            msg_status = "DISPONÍVEL"
            
            if status == "printing":
                cor_status = "printing"
                msg_status = f"IMPRIMINDO {progresso}%"
            elif status == "paused":
                cor_status = "paused"
                msg_status = "PAUSADA / ATENÇÃO"
            elif status == "complete":
                cor_status = "ready"
                msg_status = "CONCLUÍDO"
            elif status == "error" or status == "offline":
                cor_status = "offline"
                msg_status = "OFFLINE"

            IMPRESSORAS_ENCONTRADAS[ip] = {
                'nome': f"Máquina {i}", 
                'modelo_real': "Neptune 4 MAX",
                'status': status,
                'cor': cor_status,
                'msg': msg_status,
                'ip': ip,
                'imagem': "n4max.png",
                'arquivo': filename,
                'progresso': progresso
            }
            print(f"[ONLINE] {ip} - {status}")
    except:
        pass

def scanner_rede():
    print(f"--- Iniciando Scanner Turbo na rede {IP_BASE}x ---")
    while True:
        with ThreadPoolExecutor(max_workers=100) as executor:
            executor.map(verificar_ip, range(RANGE_IPS[0], RANGE_IPS[1]))
        time.sleep(3)

# Inicia o scanner em paralelo
threading.Thread(target=scanner_rede, daemon=True).start()

# --- ROTAS DO SITE ---

@app.route('/')
def index():
    impressoras_ordenadas = dict(sorted(IMPRESSORAS_ENCONTRADAS.items(), key=lambda item: int(item[0].split('.')[-1])))
    return render_template('index.html', impressoras=impressoras_ordenadas)

# ... (restante das rotas /navegar e /imprimir permanecem iguais)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)