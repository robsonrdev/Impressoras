import os
import secrets
import time
import threading
import requests
import socket
import re
import time
import urllib.parse
import json
from queue import Queue
from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

app = Flask(__name__)

# --- CONFIGURAÇÕES ---
PASTA_RAIZ = os.path.abspath(r'/app/gcodes') 
ARQUIVO_BANCO = 'impressores.json' 

UPLOAD_FILAS = {}          # ip -> Queue()
UPLOAD_WORKERS = {}        # ip -> Thread
UPLOAD_LOCK = threading.Lock()
MAX_UPLOADS_SIMULTANEOS = 1  # 1 = fila total (um upload por vez no sistema)
UPLOAD_SEM = threading.Semaphore(MAX_UPLOADS_SIMULTANEOS)

BLING_API_KEY = "seu_token_aqui"

# Configurações do Cache
cache_estoque = {
    "dados": None,
    "expira_em": 0
}
load_dotenv(dotenv_path='.env')

CLIENT_ID = os.getenv("BLING_CLIENT_ID")
CLIENT_SECRET = os.getenv("BLING_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")




# Dicionários Globais - Memória de Status
IMPRESSORAS_ENCONTRADAS = {}
PROGRESSO_UPLOAD = {} 

ARQUIVO_PRODUCAO = 'producao_diaria.json'
ULTIMO_STATUS_MAQUINAS = {} # Para detectar a transição de status

# --- ESTABILIDADE DE REDE ---
SESSAO_REDE = requests.Session()
SESSAO_REDE.headers.update({'Connection': 'keep-alive'})
FALHAS_CONSECUTIVAS = {}

def chave_ordem_maquina(m):
    """
    Ordena por número no começo do nome.
    Exemplos válidos: "1", "01", "1 - N4MAX", "10_Principal"
    Quem não tiver número vai pro fim.
    """
    nome = str(m.get("nome", "")).strip()
    match = re.match(r'^(\d+)', nome)
    if match:
        return int(match.group(1))
    return 999999  # vai pro final

def carregar_maquinas():
    """Lê a lista de máquinas e retorna ORDENADA pelo número do nome"""
    if not os.path.exists(ARQUIVO_BANCO):
        return []
    try:
        with open(ARQUIVO_BANCO, 'r') as f:
            maquinas = json.load(f)  # [{"ip": "...", "nome": "..."}, ...]
            return sorted(maquinas, key=chave_ordem_maquina)
    except:
        return []

def salvar_maquina(ip, nome):
    maquinas = carregar_maquinas()
    if not any(m['ip'] == ip for m in maquinas):
        maquinas.append({"ip": ip, "nome": nome})
        maquinas = sorted(maquinas, key=chave_ordem_maquina)
        with open(ARQUIVO_BANCO, 'w') as f:
            json.dump(maquinas, f, indent=4)
        return True
    return False

class Monitor:
    def __init__(self, file, ip_alvo):
        self.file = file
        self.total = os.path.getsize(file.name)
        self.bytes_read = 0
        self.ip_alvo = ip_alvo

        # controle de atualização (throttle)
        self._last_update = 0.0
        self._min_interval = 0.25  # 250 ms

    def read(self, size=-1):
        # força leitura em blocos maiores (menos chamadas)
        if size is None or size < 256 * 1024:
            size = 256 * 1024  # 256 KB

        data = self.file.read(size)
        self.bytes_read += len(data)

        now = time.time()
        if self.total > 0 and (now - self._last_update) >= self._min_interval:
            self._last_update = now

            percent = int((self.bytes_read / self.total) * 90)
            PROGRESSO_UPLOAD[self.ip_alvo] = {
                "p": percent,
                "msg": f"Transmitindo... ({percent}%)"
            }

        return data

    def __getattr__(self, attr):
        return getattr(self.file, attr)


# --- FUNÇÕES DE REDE E MONITORAMENTO ---
def testar_conexao_rapida(ip, porta=80):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.8)
        resultado = sock.connect_ex((ip, porta))
        sock.close()
        return resultado == 0
    except: return False

#bling 

# ==========================================================================
# SEÇÃO BLING V3 - GESTÃO CENTRALIZADA (BETIM)
# ==========================================================================

# 1. Funções de Persistência de Tokens
def carregar_tokens():
    """Lê os tokens do disco com fallback para dicionário vazio"""
    if not os.path.exists('tokens.json'):
        return {}
    try:
        with open('tokens.json', 'r') as f:
            return json.load(f)
    except:
        return {}

def salvar_tokens(tokens):
    """Salva os tokens para que todos os dispositivos usem a mesma sessão"""
    with open('tokens.json', 'w') as f:
        json.dump(tokens, f, indent=4)

# 2. Lógica de Renovação Automática (OAuth 2.0)
def garantir_token_valido():
    """Garante que qualquer dispositivo sempre tenha um token funcional"""
    tokens = carregar_tokens()
    if not tokens:
        raise Exception("Nenhum token encontrado. Acesse /login_bling primeiro.")

    agora = time.time()
    # Se o token expira em menos de 5 minutos, renova
    if agora > (tokens.get('expires_at', 0) - 300):
        print("🔄 Renovando acesso para múltiplos dispositivos...")
        url = "https://www.bling.com.br/Api/v3/oauth/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": tokens['refresh_token']
        }
        
        response = requests.post(url, data=payload, auth=(CLIENT_ID, CLIENT_SECRET))
        
        if response.status_code == 200:
            novos_dados = response.json()
            tokens['access_token'] = novos_dados['access_token']
            tokens['refresh_token'] = novos_dados.get('refresh_token', tokens['refresh_token'])
            tokens['expires_at'] = agora + novos_dados['expires_in']
            salvar_tokens(tokens)
            print("✅ Token renovado globalmente!")
        else:
            print(f"🚨 Erro na renovação: {response.text}")
    
    return tokens['access_token']

# 3. Busca de Estoque com Cache de 5 Minutos
def buscar_estoque_bling(headers):
    agora = time.time()
    if cache_estoque["dados"] and agora < cache_estoque["expira_em"]:
        return cache_estoque["dados"]

    # Rota correta para trazer NOME + FOTO + ESTOQUE (8,00)
    url = "https://www.bling.com.br/Api/v3/produtos?estoque=S"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            cache_estoque["dados"] = response.json()
            cache_estoque["expira_em"] = agora + 300
            return cache_estoque["dados"]
        return {"data": []}
    except Exception as e:
        print(f"Erro na API: {e}")
        return {"data": []}

# 4. Rotas de Autenticação e API
@app.route('/login_bling')
def login_bling():
    # 1. Gera um código aleatório para o 'state'
    state = secrets.token_hex(16) 
    
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state, # AGORA OBRIGATÓRIO
        "scope": "produtos:read estoques:read estoques:write"
    }
    url = "https://www.bling.com.br/Api/v3/oauth/authorize?" + urllib.parse.urlencode(params)
    return f"<script>window.location.href='{url}';</script>"

@app.route('/callback')
def callback():
    # 2. O Bling agora devolve o code E o state
    code = request.args.get('code')
    state = request.args.get('state') 
    
    print(f"--- DEBUG CALLBACK BETIM ---")
    print(f"Código recebido: {code}")
    print(f"Estado recebido: {state}")
    print(f"-----------------------------")

    if not code:
        # Se der erro, o Bling manda a descrição na URL
        erro = request.args.get('error_description', 'Erro desconhecido')
        return f"<h1>⚠️ Falha na Farm: {erro}</h1>", 400

    url = "https://www.bling.com.br/Api/v3/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    }
    
    response = requests.post(url, data=payload, auth=(CLIENT_ID, CLIENT_SECRET))
    
    if response.status_code == 200:
        dados = response.json()
        tokens = {
            "access_token": dados['access_token'],
            "refresh_token": dados['refresh_token'],
            "expires_at": time.time() + dados['expires_in']
        }
        salvar_tokens(tokens)
        return "<h1>✅ SuperTech 3D: Acesso Total Liberado!</h1>"
    
    return f"Erro final: {response.text}"

@app.route('/api/estoque_bling')
def pegar_estoque():
    try:
        # 1. Garante que o token OAuth 2.0 esteja atualizado
        token = garantir_token_valido()
        headers = {"Authorization": f"Bearer {token}"}
        
        # 2. Chamada para a API V3 (Filtro 'estoque=S' traz os saldos)
        url = "https://www.bling.com.br/Api/v3/produtos?estoque=S"
        response = requests.get(url, headers=headers, timeout=10)
        
        # 3. Verifica se a API do Bling respondeu com sucesso (Status 200)
        if response.status_code == 200:
            dados = response.json()
            # Retorna apenas o JSON para o front-end, sem salvar arquivos no disco
            return jsonify(dados)
        else:
            print(f"⚠️ Erro na API Bling: Status {response.status_code}")
            return jsonify({"error": "Erro na comunicação com o Bling", "details": response.text}), response.status_code

    except Exception as e:
        # Log de erro para o terminal do VS Code em Betim
        print(f"🚨 Falha crítica no módulo de estoque: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/adicionar_estoque', methods=['POST'])
def adicionar_estoque():
    dados = request.json
    try:
        token = garantir_token_valido()
        url = "https://www.bling.com.br/Api/v3/estoques"
        headers = {"Authorization": f"Bearer {token}"}
        
        payload = {
            "produto": {"id": dados.get('id')},
            "quantidade": dados.get('quantidade'),
            "operacao": "E" # Entrada manual de produção
        }
        
        response = requests.post(url, json=payload, headers=headers)
        # Invalida o cache para mostrar o novo número imediatamente
        cache_estoque["expira_em"] = 0
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

#bling 

# ==========================================================================
# NOVAS ROTAS PARA O COMMAND CENTER (ABAS DE DETALHES)
# ==========================================================================

@app.route('/api/detalhes_profundos/<ip>')
def detalhes_profundos(ip):
    """Busca Temperaturas e Console com tratamento de erro robusto"""
    try:
        # 1. Busca Temperaturas (Query Objects)
        url_status = f"http://{ip}/printer/objects/query?extruder&heater_bed&print_stats&display_status"
        resp_s = SESSAO_REDE.get(url_status, timeout=1.5) # Timeout curto para não travar o app
        
        # 2. Busca o Console (Gcode Store)
        url_console = f"http://{ip}/server/gcode_store"
        resp_c = SESSAO_REDE.get(url_console, timeout=1.5)
        
        # Verifica se as requisições foram bem sucedidas antes de processar
        # --- DENTRO DA ROTA detalhes_profundos ---
        if resp_s.status_code == 200 and resp_c.status_code == 200:
            dados_s = resp_s.json()
            dados_c = resp_c.json()
            
            # 🛡️ CORREÇÃO: Acessando a lista correta 'gcode_store' antes de fatiar
            logs_brutos = dados_c.get('result', {}).get('gcode_store', [])
            
            result = {
                "status": dados_s.get('result', {}).get('status', {}),
                "console": logs_brutos[-12:] if isinstance(logs_brutos, list) else [] 
            }
            return jsonify(result)
        else:
            print(f"⚠️ Erro de API na impressora {ip}: Status {resp_s.status_code}/{resp_c.status_code}")
            return jsonify({"error": "Impressora não respondeu corretamente"}), 400

    except Exception as e:
        # 🚨 Este log aparecerá no seu terminal do VS Code / CMD
        print(f"🚨 ERRO CRÍTICO na rota detalhes ({ip}): {str(e)}")
        return jsonify({"error": "Falha na comunicação de rede"}), 500

@app.route('/api/arquivos_internos/<ip>')
def arquivos_internos(ip):
    """Lista os arquivos salvos na memória da impressora (Trabalhos)"""
    try:
        url = f"http://{ip}/server/files/list?root=gcodes"
        resp = SESSAO_REDE.get(url, timeout=3.0).json()
        
        # Organiza os arquivos por data de modificação (mais recentes primeiro)
        arquivos = sorted(resp.get('result', []), key=lambda x: x.get('modified', 0), reverse=True)
        return jsonify(arquivos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/comando_gcode', methods=['POST'])
def comando_gcode():
    """Envia comandos manuais (Pausa, Cancela, G-Code)"""
    dados = request.json
    ip = dados.get('ip')
    comando = dados.get('comando') # Ex: "PAUSE", "CANCEL", "G28"
    
    try:
        # Traduz comandos simples para a API do Klipper
        if comando == "PAUSE": url = f"http://{ip}/printer/print/pause"
        elif comando == "RESUME": url = f"http://{ip}/printer/print/resume"
        elif comando == "CANCEL": url = f"http://{ip}/printer/print/cancel"
        else: url = f"http://{ip}/printer/gcode/script?script={urllib.parse.quote(comando)}"
        
        SESSAO_REDE.post(url, timeout=5.0)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

def verificar_ip(ip, nome_personalizado):
    """Atualiza o status e gerencia a contagem de produção"""
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
            status_klipper = dados['result']['status']['print_stats']['state']
            filename = dados['result']['status']['print_stats']['filename']
            progresso = int(dados['result']['status']['display_status']['progress'] * 100)

            # --- 🚀 LÓGICA DE PRODUÇÃO (DENTRO DA FUNÇÃO) ---
            status_anterior = ULTIMO_STATUS_MAQUINAS.get(ip)
            
            # GATILHO: Mudou de "printing" para "complete"
            if status_anterior == "printing" and status_klipper == "complete":
                if filename and filename != "Nenhum": # 🛡️ Impede contagem de erro
                    registrar_conclusao(filename)
            
            # Atualiza a memória de status para a próxima verificação
            ULTIMO_STATUS_MAQUINAS[ip] = status_klipper
            
            # Mapeamento de Status para o Dashboard Pro
            if status_klipper == "printing":
                msg_exibicao, cor_status = f"IMPRIMINDO {progresso}%", "printing"
            elif status_klipper in ["startup", "busy"]:
                msg_exibicao, cor_status = "PREPARANDO", "printing"
            elif status_klipper == "paused":
                msg_exibicao, cor_status = "PAUSADO", "paused"
            elif status_klipper == "complete":
                msg_exibicao, cor_status = "CONCLUÍDO", "ready"
            elif status_klipper in ["standby", "ready", "idle"]:
                msg_exibicao, cor_status = "PRONTA", "ready"
            else:
                msg_exibicao, cor_status = "OFFLINE", "offline"
            
            IMPRESSORAS_ENCONTRADAS[ip] = {
                'nome': nome_personalizado,
                'modelo_real': "Neptune 4 MAX",
                'status': status_klipper,
                'cor': cor_status,
                'msg': msg_exibicao,
                'ip': ip,
                'imagem': "n4max.png",
                'arquivo': filename if filename else "Nenhum",
                'progresso': progresso
            }
    except Exception as e:
        print(f"Erro ao monitorar {ip}: {e}")

def monitor_inteligente():
    """Monitora as máquinas respeitando a ordem do arquivo"""
    while True:
        maquinas = carregar_maquinas()
        if maquinas:
            with ThreadPoolExecutor(max_workers=15) as executor:
                for m in maquinas:
                    executor.submit(verificar_ip, m['ip'], m['nome'])
        time.sleep(3)

threading.Thread(target=monitor_inteligente, daemon=True).start()


"""Fila de impressão """
def garantir_fila(ip):
    with UPLOAD_LOCK:
        if ip not in UPLOAD_FILAS:
            UPLOAD_FILAS[ip] = Queue()
        if ip not in UPLOAD_WORKERS or not UPLOAD_WORKERS[ip].is_alive():
            t = threading.Thread(target=worker_upload, args=(ip,), daemon=True)
            UPLOAD_WORKERS[ip] = t
            t.start()

def worker_upload(ip):
    """
    Processa uploads 1 por vez para esse IP.
    """
    fila = UPLOAD_FILAS[ip]
    while True:
        job = fila.get()  # bloqueia até chegar item
        if job is None:
            fila.task_done()
            break

        caminho = job.get("caminho")
        nome_exib = job.get("arquivo_label", os.path.basename(caminho))

        # mostra status de fila -> enviando
        PROGRESSO_UPLOAD[ip] = {"p": 0, "msg": f"Na fila: {nome_exib}"}

        try:
            tarefa_upload(ip, caminho)
        except Exception:
            PROGRESSO_UPLOAD[ip] = {"p": -1, "msg": "Erro de Rede"}
        finally:
            fila.task_done()

def enfileirar_impressao(ip, caminho_completo, arquivo_label=None):
    garantir_fila(ip)

    # tamanho da fila (posição)
    pos = UPLOAD_FILAS[ip].qsize() + 1
    label = arquivo_label or os.path.basename(caminho_completo)

    PROGRESSO_UPLOAD[ip] = {"p": 0, "msg": f"Na fila (pos {pos}): {label}"}

    UPLOAD_FILAS[ip].put({
        "caminho": caminho_completo,
        "arquivo_label": label
    })


"""Fila de impressão """

# --- LÓGICA DE UPLOAD ---
def tarefa_upload(ip_alvo, caminho_completo):
    nome_arquivo = os.path.basename(caminho_completo)

    try:
        # ocupa 1 "slot" global de upload
        with UPLOAD_SEM:
            PROGRESSO_UPLOAD[ip_alvo] = {"p": 1, "msg": "Enviando arquivo..."}

            with open(caminho_completo, 'rb') as f:
                monitor = Monitor(f, ip_alvo)
                files = {'file': (nome_arquivo, monitor)}

                resp = SESSAO_REDE.post(
                    f"http://{ip_alvo}/server/files/upload",
                    files=files,
                    timeout=900
                )
                resp.raise_for_status()

        time.sleep(1.0)
        PROGRESSO_UPLOAD[ip_alvo] = {"p": 95, "msg": "Iniciando..."}

        nome_url = urllib.parse.quote(nome_arquivo)
        try:
            SESSAO_REDE.post(
                f"http://{ip_alvo}/printer/print/start?filename={nome_url}",
                timeout=10
            )
        except:
            pass

        time.sleep(0.5)
        PROGRESSO_UPLOAD[ip_alvo] = {"p": 100, "msg": "Sucesso!"}

    except Exception as e:
        PROGRESSO_UPLOAD[ip_alvo] = {"p": -1, "msg": f"Erro: {str(e)[:60]}"}



def registrar_conclusao(nome_arquivo):
    """🛡️ Registra a peça e salva no disco"""
    if not nome_arquivo or nome_arquivo == "Nenhum": 
        return
        
    dados = carregar_producao_24h()
    nome_limpo = nome_arquivo.replace('.gcode', '').replace('.bgcode', '')
    
    # Incrementa o contador da peça específica
    dados["itens"][nome_limpo] = dados["itens"].get(nome_limpo, 0) + 1
    
    # Salva fisicamente para persistência em Betim
    try:
        with open(ARQUIVO_PRODUCAO, 'w') as f:
            json.dump(dados, f, indent=4)
    except Exception as e:
        print(f"Erro ao salvar produção: {e}")

# --- ROTAS FLASK ---
@app.route('/')
def index():
    """Renderiza as máquinas na ordem exata do arquivo JSON"""
    maquinas_cadastradas = carregar_maquinas()
    lista_final = {}
    
    # Reconstrói o dicionário na ordem do JSON para o Template
    for m in maquinas_cadastradas:
        ip = m['ip']
        if ip in IMPRESSORAS_ENCONTRADAS:
            lista_final[ip] = IMPRESSORAS_ENCONTRADAS[ip]
        else:
            # Caso a máquina ainda não tenha sido monitorada no primeiro boot
            lista_final[ip] = {'nome': m['nome'], 'ip': ip, 'status': 'offline', 'cor': 'offline', 'msg': 'CONECTANDO...', 'progresso': 0, 'imagem': 'n4max.png', 'modelo_real': 'Neptune 4 MAX'}

    disponiveis = sum(1 for p in IMPRESSORAS_ENCONTRADAS.values() if p.get('status') in ['ready', 'idle'])
    return render_template('index.html', impressoras=lista_final, disponiveis=disponiveis)

@app.route('/cadastrar_impressora', methods=['POST'])
def cadastrar_impressora():
    dados = request.json
    ip, nome = dados.get('ip'), dados.get('nome')
    if ip and nome and salvar_maquina(ip, nome):
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "IP já existe ou dados inválidos"})

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

    enfileirar_impressao(ip, caminho, arquivo_label=os.path.basename(caminho))
    return jsonify({"success": True, "queued": True})

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

@app.route('/remover_impressora', methods=['POST'])
def remover_impressora():
    ip = request.json.get('ip')
    maquinas = carregar_maquinas()
    
    # Filtra a lista removendo o IP selecionado
    novas_maquinas = [m for m in maquinas if m['ip'] != ip]

    if len(novas_maquinas) < len(maquinas):
        # Salva a nova lista no banco de dados
        with open(ARQUIVO_BANCO, 'w') as f:
            json.dump(novas_maquinas, f, indent=4)
        
        # Remove da memória de monitoramento
        if ip in IMPRESSORAS_ENCONTRADAS:
            del IMPRESSORAS_ENCONTRADAS[ip]
            
        return jsonify({"success": True})
    
    return jsonify({"success": False, "message": "Impressora não encontrada"})

def carregar_producao_24h():
    if not os.path.exists(ARQUIVO_PRODUCAO):
        return {"data": time.strftime("%Y-%m-%d"), "itens": {}}
    with open(ARQUIVO_PRODUCAO, 'r') as f:
        dados = json.load(f)
        # Reseta se mudar o dia
        if dados.get("data") != time.strftime("%Y-%m-%d"):
            return {"data": time.strftime("%Y-%m-%d"), "itens": {}}
        return dados


@app.route('/dados_producao_diaria')
def dados_producao_diaria():
    """Envia os dados de contagem para o widget 'Concluídos (24h)'"""
    return jsonify(carregar_producao_24h())

"""Ações em massa"""
@app.route('/api/comando_gcode_em_massa', methods=['POST'])
def comando_gcode_em_massa():
    dados = request.json or {}
    ips = dados.get('ips', [])
    comando = dados.get('comando', '')

    if not ips or not comando:
        return jsonify({"success": False, "message": "ips/comando ausentes"}), 400

    def enviar(ip):
        try:
            if comando == "PAUSE": url = f"http://{ip}/printer/print/pause"
            elif comando == "RESUME": url = f"http://{ip}/printer/print/resume"
            elif comando == "CANCEL": url = f"http://{ip}/printer/print/cancel"
            else: url = f"http://{ip}/printer/gcode/script?script={urllib.parse.quote(comando)}"
            requests.post(url, timeout=5.0)
            return True
        except:
            return False

    ok = 0
    with ThreadPoolExecutor(max_workers=15) as ex:
        results = list(ex.map(enviar, ips))
        ok = sum(1 for r in results if r)

    return jsonify({"success": True, "ok": ok, "total": len(ips)})


@app.route('/imprimir_em_massa', methods=['POST'])
def imprimir_em_massa():
    dados = request.json or {}
    ips = dados.get('ips', [])
    arquivo = (dados.get('arquivo') or '').strip()

    if not ips or not arquivo:
        return jsonify({"success": False, "message": "ips/arquivo ausentes"}), 400

    caminho = os.path.abspath(os.path.join(PASTA_RAIZ, arquivo))
    if not os.path.exists(caminho):
        return jsonify({"success": False, "message": "Arquivo não encontrado"}), 404

    for ip in ips:
        enfileirar_impressao(ip, caminho, arquivo_label=os.path.basename(caminho))

    return jsonify({"success": True, "queued": True, "total": len(ips)})

"""Ações em massa"""


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)