import os
import time
import threading
import requests
import socket
from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- CONFIGURAÇÕES ---
# Pasta onde ficam seus arquivos GCODE
PASTA_RAIZ = r'H:\Outros computadores\Meu laptop\Empresas\3D Super Tech\Design e Projetos 3D\2 - 3D Super Tech\Filtro para Umidificador'

# Configuração da Rede (Ajustado para sua nova rede 192.168.1.x)
IP_BASE = "192.168.1."   
RANGE_IPS = (1, 255)     

# Dicionário global para guardar os dados das impressoras em tempo real
IMPRESSORAS_ENCONTRADAS = {}

# --- FUNÇÃO 1: VERIFICAÇÃO RÁPIDA DE PORTA (SOCKET) ---
def testar_conexao_rapida(ip, porta=80):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.2) # 200ms para não lentificar o scan
        resultado = sock.connect_ex((ip, porta))
        sock.close()
        return resultado == 0
    except:
        return False

# --- FUNÇÃO 2: SCANNER E COLETA DE DADOS (MOONRAKER/KLIPPER) ---
def verificar_ip(i):
    ip = f"{IP_BASE}{i}"
    
    # Se a porta 80 não responder, nem tentamos o resto
    if not testar_conexao_rapida(ip):
        if ip in IMPRESSORAS_ENCONTRADAS:
            # Se ela existia e sumiu, marca como offline
            IMPRESSORAS_ENCONTRADAS[ip].update({
                'status': 'offline', 
                'cor': 'offline', 
                'msg': 'OFFLINE',
                'progresso': 0
            })
        return

    # Se a porta está aberta, tentamos pegar os dados do Klipper
    url = f"http://{ip}/printer/objects/query?print_stats&display_status"
    try:
        resp = requests.get(url, timeout=1.5)
        if resp.status_code == 200:
            dados = resp.json()
            status = dados['result']['status']['print_stats']['state']
            filename = dados['result']['status']['print_stats']['filename']
            progresso_raw = dados['result']['status']['display_status']['progress']
            progresso = int(progresso_raw * 100)
            
            # Lógica de status amigável
            cor_status = "ready"
            msg_status = "DISPONÍVEL"
            
            if status == "printing":
                cor_status = "printing"
                msg_status = f"IMPRIMINDO {progresso}%"
            elif status == "paused":
                cor_status = "paused"
                msg_status = "PAUSADA"
            elif status == "complete" or (status == "standby" and progresso >= 99):
                cor_status = "ready"
                msg_status = "CONCLUÍDO / REMOVER"
            elif status == "error":
                cor_status = "offline"
                msg_status = "ERRO"

            # Atualiza ou insere no dicionário
            IMPRESSORAS_ENCONTRADAS[ip] = {
                'nome': f"MÁQUINA {i}", 
                'modelo_real': "Neptune 4 MAX",
                'status': status,
                'cor': cor_status,
                'msg': msg_status,
                'ip': ip,
                'imagem': "n4max.png",
                'arquivo': filename if filename else "Nenhum",
                'progresso': progresso
            }
    except:
        pass

# --- FUNÇÃO 3: LOOP DO SCANNER (DAEMON) ---
def scanner_rede():
    print(f"--- Scanner Ativo na rede {IP_BASE}x ---")
    while True:
        with ThreadPoolExecutor(max_workers=50) as executor:
            executor.map(verificar_ip, range(RANGE_IPS[0], RANGE_IPS[1]))
        time.sleep(5) # Espera 5 segundos para a próxima varredura

# Inicia o scanner em uma thread separada para não travar o site
threading.Thread(target=scanner_rede, daemon=True).start()

# --- ROTAS FLASK ---

@app.route('/')
def index():
    # Ordena as impressoras pelo IP para não ficarem pulando de lugar na tela
    impressoras_ordenadas = dict(sorted(IMPRESSORAS_ENCONTRADAS.items(), key=lambda item: int(item[0].split('.')[-1])))
    return render_template('index.html', impressoras=impressoras_ordenadas)

@app.route('/navegar')
def navegar():
    # Lista apenas arquivos .gcode ou .bgcode na pasta raiz
    try:
        arquivos = [f for f in os.listdir(PASTA_RAIZ) if f.endswith(('.gcode', '.bgcode'))]
        return jsonify(arquivos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/imprimir', methods=['POST'])
def imprimir():
    dados = request.json
    ip_alvo = dados.get('ip')
    nome_arquivo = dados.get('arquivo')
    caminho_completo = os.path.join(PASTA_RAIZ, nome_arquivo)

    if not os.path.exists(caminho_completo):
        return jsonify({"success": False, "message": "Arquivo não encontrado no HD"}), 404

    try:
        # 1. Enviar o arquivo para a impressora (Upload via API Moonraker)
        url_upload = f"http://{ip_alvo}/server/files/upload"
        with open(caminho_completo, 'rb') as f:
            files = {'file': (nome_arquivo, f)}
            res_up = requests.post(url_upload, files=files, timeout=10)
        
        if res_up.status_code == 201 or res_up.status_code == 200:
            # 2. Comando para iniciar a impressão imediatamente
            url_print = f"http://{ip_alvo}/printer/print/start?filename={nome_arquivo}"
            requests.post(url_print, timeout=5)
            return jsonify({"success": True, "message": f"Imprimindo {nome_arquivo}!"})
        else:
            return jsonify({"success": False, "message": "Falha no upload para a impressora"})
            
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

if __name__ == '__main__':
    # Roda na porta 5000 (o túnel Cloudflare já está apontado para cá)
    app.run(host='0.0.0.0', port=5000, debug=False)