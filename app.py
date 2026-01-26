import os
import time
import threading
import requests
import socket
from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- CONFIGURAÇÕES ---
# Caminho restrito para segurança da rede da empresa
PASTA_RAIZ = os.path.abspath(r'\\3DSUPERTECHSERVER\Arquivos\gcodes')

# Configuração da Rede (192.168.1.x)
IP_BASE = "192.168.1."  
RANGE_IPS = (1, 255)     

# Dicionário global (mantém as máquinas fixas para evitar "pulos" na UI)
IMPRESSORAS_ENCONTRADAS = {}

# --- FUNÇÃO 1: TESTE DE PORTA (TIMEOUT AJUSTADO) ---
def testar_conexao_rapida(ip, porta=80):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 0.4s é o tempo ideal para evitar falsos offlines em redes locais
        sock.settimeout(0.4) 
        resultado = sock.connect_ex((ip, porta))
        sock.close()
        return resultado == 0
    except:
        return False

# --- FUNÇÃO 2: COLETA DE DADOS (COM RETRY) ---
def verificar_ip(i):
    ip = f"{IP_BASE}{i}"
    
    # Se falhar uma vez, tenta de novo rápido antes de marcar como offline
    if not testar_conexao_rapida(ip):
        time.sleep(0.1)
        if not testar_conexao_rapida(ip):
            if ip in IMPRESSORAS_ENCONTRADAS:
                IMPRESSORAS_ENCONTRADAS[ip].update({
                    'status': 'offline', 
                    'cor': 'offline', 
                    'msg': 'OFFLINE',
                    'progresso': 0
                })
            return

    url = f"http://{ip}/printer/objects/query?print_stats&display_status"
    try:
        resp = requests.get(url, timeout=1.5)
        if resp.status_code == 200:
            dados = resp.json()
            status = dados['result']['status']['print_stats']['state']
            filename = dados['result']['status']['print_stats']['filename']
            progresso = int(dados['result']['status']['display_status']['progress'] * 100)
            
            # Lógica de cores para o CSS
            cor_status = "printing" if status == "printing" else "ready"
            if status == "paused": cor_status = "paused"
            if status == "error": cor_status = "offline"

            # Atualiza o dicionário sem deletar nada (estabilidade da grade)
            IMPRESSORAS_ENCONTRADAS[ip] = {
                'nome': f"MÁQUINA {i}", 
                'modelo_real': "Neptune 4 MAX",
                'status': status,
                'cor': cor_status,
                'msg': f"IMPRIMINDO {progresso}%" if status == "printing" else status.upper(),
                'ip': ip,
                'imagem': "n4max.png",
                'arquivo': filename if filename else "Nenhum",
                'progresso': progresso
            }
    except:
        pass

# --- FUNÇÃO 3: SCANNER EM SEGUNDO PLANO ---
def scanner_rede():
    while True:
        with ThreadPoolExecutor(max_workers=100) as executor:
            executor.map(verificar_ip, range(RANGE_IPS[0], RANGE_IPS[1]))
        time.sleep(1) # Atualização rápida para status instantâneo

threading.Thread(target=scanner_rede, daemon=True).start()

# --- ROTAS FLASK ---

@app.route('/')
def index():
    # Ordena pelo IP para a primeira carga da página
    impressoras_ordenadas = dict(sorted(IMPRESSORAS_ENCONTRADAS.items(), key=lambda item: int(item[0].split('.')[-1])))
    return render_template('index.html', impressoras=impressoras_ordenadas)

@app.route('/status_atualizado')
def status_atualizado():
    return jsonify(IMPRESSORAS_ENCONTRADAS)

@app.route('/navegar', methods=['POST'])
def navegar():
    try:
        dados = request.json
        subpasta = dados.get('pasta', '').strip('\\/')
        caminho_alvo = os.path.abspath(os.path.join(PASTA_RAIZ, subpasta))
        
        if not caminho_alvo.startswith(PASTA_RAIZ):
            return jsonify({"erro": "Acesso Negado"}), 403

        itens = os.listdir(caminho_alvo)
        pastas = [f for f in itens if os.path.isdir(os.path.join(caminho_alvo, f))]
        arquivos = [f for f in itens if f.endswith(('.gcode', '.bgcode'))]
        
        return jsonify({"atual": subpasta, "pastas": sorted(pastas), "arquivos": sorted(arquivos)})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route('/imprimir', methods=['POST'])
def imprimir():
    dados = request.json
    ip_alvo = dados.get('ip')
    nome_arquivo = dados.get('arquivo')
    caminho_completo = os.path.abspath(os.path.join(PASTA_RAIZ, nome_arquivo))

    if not caminho_completo.startswith(PASTA_RAIZ) or not os.path.exists(caminho_completo):
        return jsonify({"success": False, "message": "Arquivo não encontrado"}), 404

    try:
        url_upload = f"http://{ip_alvo}/server/files/upload"
        nome_curto = os.path.basename(caminho_completo)
        
        with open(caminho_completo, 'rb') as f:
            files = {'file': (nome_curto, f)}
            res_up = requests.post(url_upload, files=files, timeout=60)
        
        if res_up.status_code in [200, 201]:
            url_print = f"http://{ip_alvo}/printer/print/start?filename={nome_curto}"
            requests.post(url_print, timeout=5)
            return jsonify({"success": True, "message": "Iniciando!"})
        return jsonify({"success": False, "message": "Erro no upload"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)