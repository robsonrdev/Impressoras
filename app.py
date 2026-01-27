import os
import time
import threading
import requests
import socket
import urllib.parse
from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- CONFIGURAÇÕES ---
PASTA_RAIZ = os.path.abspath(r'/app/gcodes') 
IP_BASE = "192.168.1."
RANGE_IPS = (1, 255) # Otimizado para sua rede (ignora IPs abaixo de 40)

# Dicionários Globais - Memória Permanente da Farm
IMPRESSORAS_ENCONTRADAS = {}
PROGRESSO_UPLOAD = {} 

# --- ESTABILIDADE DE REDE ---
SESSAO_REDE = requests.Session()
SESSAO_REDE.headers.update({'Connection': 'keep-alive'})
FALHAS_CONSECUTIVAS = {}

# --- CLASSE MONITOR (TRANSMISSÃO COM BUFFER DE VALIDAÇÃO) ---
class Monitor:
    def __init__(self, file, ip_alvo):
        self.file = file
        self.total = os.path.getsize(file.name)
        self.bytes_read = 0
        self.ip_alvo = ip_alvo

    def read(self, size=-1):
        data = self.file.read(size)
        self.bytes_read += len(data)
        if self.total > 0:
            # Trava em 90% para deixar margem para a verificação de metadados
            percent = int((self.bytes_read / self.total) * 90)
            if self.ip_alvo in PROGRESSO_UPLOAD:
                PROGRESSO_UPLOAD[self.ip_alvo]["p"] = percent
                PROGRESSO_UPLOAD[self.ip_alvo]["msg"] = f"Transmitindo... ({percent}%)"
        return data

    def __getattr__(self, attr):
        return getattr(self.file, attr)

# --- FUNÇÕES DE REDE ---
def testar_conexao_rapida(ip, porta=80):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.8)
        resultado = sock.connect_ex((ip, porta))
        sock.close()
        return resultado == 0
    except: return False

def verificar_ip(i):
    """Mapeia e atualiza status com tolerância a falhas (Memória Fixa)"""
    ip = f"{IP_BASE}{i}"
    
    if not testar_conexao_rapida(ip):
        if ip in IMPRESSORAS_ENCONTRADAS:
            FALHAS_CONSECUTIVAS[ip] = FALHAS_CONSECUTIVAS.get(ip, 0) + 1
            if FALHAS_CONSECUTIVAS[ip] >= 3:
                IMPRESSORAS_ENCONTRADAS[ip].update({
                    'status': 'offline', 'cor': 'offline', 'msg': 'OFFLINE', 'progresso': 0
                })
        return

    url = f"http://{ip}/printer/objects/query?print_stats&display_status"
    try:
        resp = SESSAO_REDE.get(url, timeout=2.0)
        if resp.status_code == 200:
            FALHAS_CONSECUTIVAS[ip] = 0
            dados = resp.json()
            status = dados['result']['status']['print_stats']['state']
            filename = dados['result']['status']['print_stats']['filename']
            progresso = int(dados['result']['status']['display_status']['progress'] * 100)
            
            cor_status = "printing" if status == "printing" else "ready"
            if status == "paused": cor_status = "paused"
            
            msg_exibicao = "STANDBAY"
            if status == "printing":
                msg_exibicao = f"IMPRIMINDO {progresso}%"
            
            IMPRESSORAS_ENCONTRADAS[ip] = {
                'nome': f"MÁQUINA {i}",
                'modelo_real': "Neptune 4 MAX",
                'status': status,
                'cor': cor_status,
                'msg': msg_exibicao,
                'ip': ip,
                'imagem': "n4max.png",
                'arquivo': filename if filename else "Nenhum",
                'progresso': progresso
            }
    except: 
        if ip in IMPRESSORAS_ENCONTRADAS:
            FALHAS_CONSECUTIVAS[ip] = FALHAS_CONSECUTIVAS.get(ip, 0) + 1
            if FALHAS_CONSECUTIVAS[ip] >= 3:
                IMPRESSORAS_ENCONTRADAS[ip].update({'status': 'offline', 'cor': 'offline', 'msg': 'OFFLINE'})

def scanner_inteligente():
    """Varre a rede uma vez e monitora os IPs encontrados (Memória)"""
    with ThreadPoolExecutor(max_workers=50) as executor:
        executor.map(verificar_ip, range(RANGE_IPS[0], RANGE_IPS[1]))
    
    while True:
        time.sleep(3)
        if IMPRESSORAS_ENCONTRADAS:
            ips_memoria = [int(ip.split('.')[-1]) for ip in IMPRESSORAS_ENCONTRADAS.keys()]
            with ThreadPoolExecutor(max_workers=15) as executor:
                executor.map(verificar_ip, ips_memoria)

threading.Thread(target=scanner_inteligente, daemon=True).start()

# --- LÓGICA DE UPLOAD COM AUTO-RETRY E INTEGRIDADE ---
def tarefa_upload(ip_alvo, caminho_completo):
    tentativas_max = 2
    for tentativa in range(tentativas_max):
        try:
            nome_arquivo = os.path.basename(caminho_completo)
            tamanho_local = os.path.getsize(caminho_completo)
            
            # Feedback Visual: Etapa de Validação
            PROGRESSO_UPLOAD[ip_alvo] = {"p": 5, "msg": f"Tentativa {tentativa+1}: Validando..."}
            
            # 1. Verificação de prontidão
            check = SESSAO_REDE.get(f"http://{ip_alvo}/printer/info", timeout=5).json()
            if check.get('result', {}).get('state') not in ['ready', 'idle']:
                PROGRESSO_UPLOAD[ip_alvo] = {"p": -1, "msg": "ERRO: Máquina Ocupada"}
                return

            # 2. Upload com Monitor de progresso
            with open(caminho_completo, 'rb') as f:
                monitor = Monitor(f, ip_alvo)
                files = {'file': (nome_arquivo, monitor)}
                SESSAO_REDE.post(f"http://{ip_alvo}/server/files/upload", files=files, timeout=900)
            
            # 3. Verificação de Integridade (Metadata)
            PROGRESSO_UPLOAD[ip_alvo] = {"p": 95, "msg": "Verificando integridade..."}
            time.sleep(2)
            meta_url = f"http://{ip_alvo}/server/files/metadata?filename={urllib.parse.quote(nome_arquivo)}"
            meta_resp = SESSAO_REDE.get(meta_url, timeout=5).json()
            tamanho_remoto = meta_resp.get('result', {}).get('size', 0)

            if tamanho_local == tamanho_remoto:
                # 4. Início seguro
                PROGRESSO_UPLOAD[ip_alvo] = {"p": 98, "msg": "Integridade OK! Iniciando..."}
                nome_url = urllib.parse.quote(nome_arquivo)
                res_print = SESSAO_REDE.post(f"http://{ip_alvo}/printer/print/start?filename={nome_url}", timeout=10)
                
                if res_print.status_code == 200:
                    PROGRESSO_UPLOAD[ip_alvo] = {"p": 100, "msg": "Sucesso! Bom trabalho."}
                    return 
                else:
                    PROGRESSO_UPLOAD[ip_alvo] = {"p": -1, "msg": "ERRO: Falha no Start"}
                    return
            else:
                # Falha na integridade: Tenta retransmitir se houver tentativas sobrando
                if tentativa < tentativas_max - 1:
                    PROGRESSO_UPLOAD[ip_alvo]["msg"] = "Erro de integridade. Reiniciando..."
                    time.sleep(1)
                else:
                    PROGRESSO_UPLOAD[ip_alvo] = {"p": -1, "msg": "ERRO: Arquivo Corrompido"}

        except Exception as e:
            if tentativa == tentativas_max - 1:
                PROGRESSO_UPLOAD[ip_alvo] = {"p": -1, "msg": "FALHA: Erro de Rede"}

# --- ROTAS FLASK ---
@app.route('/')
def index():
    ordenadas = dict(sorted(IMPRESSORAS_ENCONTRADAS.items(), key=lambda item: int(item[0].split('.')[-1])))
    disponiveis = sum(1 for p in IMPRESSORAS_ENCONTRADAS.values() if p.get('status') in ['ready', 'idle'])
    return render_template('index.html', impressoras=ordenadas, disponiveis=disponiveis)

@app.route('/status_atualizado')
def status_atualizado():
    disponiveis = sum(1 for p in IMPRESSORAS_ENCONTRADAS.values() if p.get('status') in ['ready', 'idle'])
    return jsonify({"impressoras": IMPRESSORAS_ENCONTRADAS, "total_disponiveis": disponiveis})

@app.route('/imprimir', methods=['POST'])
def imprimir():
    dados = request.json
    ip, arquivo = dados.get('ip'), dados.get('arquivo')
    caminho = os.path.abspath(os.path.join(PASTA_RAIZ, arquivo))
    if not os.path.exists(caminho):
        return jsonify({"success": False, "message": "Arquivo não encontrado"})
    
    PROGRESSO_UPLOAD[ip] = {"p": 0, "msg": "Preparando..."}
    threading.Thread(target=tarefa_upload, args=(ip, caminho)).start()
    return jsonify({"success": True})

@app.route('/progresso_transmissao/<ip>')
def progresso_transmissao(ip):
    return jsonify(PROGRESSO_UPLOAD.get(ip, {"p": 0, "msg": "..."}))

@app.route('/navegar', methods=['POST'])
def navegar():
    dados = request.json
    subpasta = dados.get('pasta', '').strip('\\/')
    caminho_alvo = os.path.abspath(os.path.join(PASTA_RAIZ, subpasta))
    itens = os.listdir(caminho_alvo)
    return jsonify({
        "atual": subpasta,
        "pastas": sorted([f for f in itens if os.path.isdir(os.path.join(caminho_alvo, f))]),
        "arquivos": sorted([f for f in itens if f.endswith(('.gcode', '.bgcode'))])
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)