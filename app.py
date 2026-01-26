import os
import time
import threading
import requests
import socket
from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- CONFIGURAÇÕES ---
# Caminho dentro do contêiner Docker
PASTA_RAIZ = os.path.abspath(r'/app/gcodes') 
IP_BASE = "192.168.1."
RANGE_IPS = (1, 255)

# Dicionários Globais
IMPRESSORAS_ENCONTRADAS = {}
PROGRESSO_UPLOAD = {} # Armazena a porcentagem de envio (0 a 100)

# --- CLASSE MONITOR (CORRIGIDA) ---
class Monitor:
    def __init__(self, file, ip_alvo):
        self.file = file
        self.total = os.path.getsize(file.name)
        self.bytes_read = 0
        self.ip_alvo = ip_alvo

    def read(self, size=-1): # Adicionado size=-1 para evitar TypeError
        data = self.file.read(size)
        self.bytes_read += len(data)
        
        if self.total > 0:
            percent = int((self.bytes_read / self.total) * 100)
            PROGRESSO_UPLOAD[self.ip_alvo] = percent
            
        return data

    def __getattr__(self, attr):
        return getattr(self.file, attr)

# --- FUNÇÕES DE REDE ---
def testar_conexao_rapida(ip, porta=80):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.4)
        resultado = sock.connect_ex((ip, porta))
        sock.close()
        return resultado == 0
    except: 
        return False

def verificar_ip(i):
    ip = f"{IP_BASE}{i}"
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
            
            cor_status = "printing" if status == "printing" else "ready"
            if status == "paused": cor_status = "paused"
            
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

def scanner_rede():
    while True:
        with ThreadPoolExecutor(max_workers=50) as executor:
            executor.map(verificar_ip, range(RANGE_IPS[0], RANGE_IPS[1]))
        time.sleep(2)

# Inicia o scanner em segundo plano
threading.Thread(target=scanner_rede, daemon=True).start()

# --- FUNÇÃO DE BACKGROUND PARA UPLOAD (CORRIGIDA) ---
def tarefa_upload(ip_alvo, caminho_completo):
    """Executa o upload em uma thread separada para não travar o servidor/túnel"""
    try:
        url_upload = f"http://{ip_alvo}/server/files/upload"
        nome_curto = os.path.basename(caminho_completo)
        
        with open(caminho_completo, 'rb') as f:
            monitor = Monitor(f, ip_alvo)
            files = {'file': (nome_curto, monitor)}
            # Timeout longo para arquivos GCODE pesados
            res_up = requests.post(url_upload, files=files, timeout=600)
        
        if res_up.status_code in [200, 201]:
            # Inicia a impressão automaticamente após o upload
            url_print = f"http://{ip_alvo}/printer/print/start?filename={nome_curto}"
            requests.post(url_print, timeout=10)
            PROGRESSO_UPLOAD[ip_alvo] = 100
        else:
            PROGRESSO_UPLOAD[ip_alvo] = -1
    except Exception as e:
        print(f"Erro upload para {ip_alvo}: {e}")
        PROGRESSO_UPLOAD[ip_alvo] = -1

# --- ROTAS FLASK ---

@app.route('/')
def index():
    # Ordena as impressoras pelo último octeto do IP
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
            return jsonify({"erro": "Negado"}), 403
            
        itens = os.listdir(caminho_alvo)
        return jsonify({
            "atual": subpasta,
            "pastas": sorted([f for f in itens if os.path.isdir(os.path.join(caminho_alvo, f))]),
            "arquivos": sorted([f for f in itens if f.endswith(('.gcode', '.bgcode'))])
        })
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route('/imprimir', methods=['POST'])
def imprimir():
    dados = request.json
    ip = dados.get('ip')
    arquivo = dados.get('arquivo')
    caminho = os.path.abspath(os.path.join(PASTA_RAIZ, arquivo))
    
    if not os.path.exists(caminho):
        return jsonify({"success": False, "message": "Arquivo não encontrado"})

    # Reseta o progresso e inicia a thread de upload
    PROGRESSO_UPLOAD[ip] = 0
    threading.Thread(target=tarefa_upload, args=(ip, caminho)).start()
    
    return jsonify({"success": True})

@app.route('/progresso_transmissao/<ip>')
def progresso_transmissao(ip):
    # Retorna o progresso atual do upload para o frontend
    return jsonify({"progresso": PROGRESSO_UPLOAD.get(ip, 0)})

if __name__ == '__main__':
    # Rodando na porta 5000 para o Docker
    app.run(host='0.0.0.0', port=5000)