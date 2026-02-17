import os
import secrets
import time
import threading
import requests
import socket
import platform
import re
import time
import urllib.parse
import json
from queue import Queue
from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from datetime import timezone
from datetime import datetime


app = Flask(__name__)


# --- CONFIGURAÇÕES ---
PASTA_RAIZ = os.path.abspath(r'/app/gcodes') 

basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'farm_supertech.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(BASE_DIR, exist_ok=True)
TOKENS_PATH = os.path.join(BASE_DIR, "tokens.json")

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


class Maquina(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip = db.Column(db.String(20), unique=True, nullable=False) # IP único para não duplicar
    nome = db.Column(db.String(50), nullable=False)           # Nome da Neptune 4 MAX
    modelo = db.Column(db.String(50), default="Neptune 4 MAX") # Modelo padrão
    imagem = db.Column(db.String(100), default="n4max.png")   # Nome da imagem na pasta static

# MODELO: Tabela que salva o histórico de peças produzidas
class RegistroProducao(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, default=datetime.utcnow)        # Data da conclusão
    nome_peca = db.Column(db.String(200), nullable=False)     # Nome do arquivo G-Code
    quantidade = db.Column(db.Integer, default=1)             # Quantos ciclos foram feitos

# COMANDO: Cria as tabelas fisicamente no arquivo .db ao iniciar o app
with app.app_context():
    db.create_all()

# --- Inicio Funcao Auxiliar de Ordenacao ---
def chave_ordem_maquina(m):
    """
    Mantém a sua lógica original:
    Ordena por número no começo do nome (Ex: "01 - Direita").
    Quem não tiver número vai para o final (999999).
    """
    # Se 'm' for um objeto do banco, pegamos o atributo .nome
    # Se for um dicionário (vido do monitor), usamos .get
    nome_bruto = getattr(m, 'nome', m.get("nome", "")) if not isinstance(m, dict) else m.get("nome", "")
    nome = str(nome_bruto).strip()
    
    match = re.match(r'^(\d+)', nome)
    if match:
        return int(match.group(1))
    return 999999  
# --- Fim Funcao Auxiliar de Ordenacao ---

# Define o caminho de acordo com o sistema operacional
if platform.system() == "Windows":
    # Modo Dev: Unidade Z: mapeada (IP .172)
    PASTA_RAIZ = os.path.normpath(r'Z:/')
else:
    # Modo Produção: Caminho real do Samba no Ubuntu
    PASTA_RAIZ = '/srv/samba/empresa/gcodes'

# Força o Python a validar se a pasta existe antes de começar
if not os.path.exists(PASTA_RAIZ):
    print(f"🚨 ERRO CRÍTICO: A pasta {PASTA_RAIZ} não foi encontrada no servidor!")

# --- Inicio Funcao Carregar Maquinas ---
def carregar_maquinas():
    """
    Busca todas as impressoras no banco SQLite e retorna 
    uma lista de dicionários ORDENADA para o seu front-end.
    """
    try:
        # Busca todos os registros na tabela Maquina
        maquinas_db = Maquina.query.all()
        
        # Converte os objetos do banco para o formato de dicionário que seu código já usa
        lista_maquinas = []
        for m in maquinas_db:
            lista_maquinas.append({
                "ip": m.ip,
                "nome": m.nome,
                "modelo": m.modelo,
                "imagem": m.imagem
            })
        
        # Retorna a lista usando a sua regra de ordenação por número no nome
        return sorted(lista_maquinas, key=chave_ordem_maquina)
    except Exception as e:
        print(f"🚨 Erro ao ler banco de dados em Betim: {e}")
        return []
# --- Fim Funcao Carregar Maquinas ---

# --- Inicio Funcao Salvar Maquina ---
def salvar_maquina(ip, nome):
    """
    Adiciona uma nova impressora ao banco de dados se o IP não existir.
    Elimina a necessidade de ler/escrever o arquivo JSON inteiro.
    """
    try:
        # Verifica se já existe uma máquina com esse IP no SQLite
        existente = Maquina.query.filter_by(ip=ip).first()
        
        if not existente:
            # Cria o novo registro
            nova_maquina = Maquina(ip=ip, nome=nome)
            
            # Adiciona e salva (commit) no arquivo .db
            db.session.add(nova_maquina)
            db.session.commit()
            print(f"✅ {nome} ({ip}) salva com sucesso no SQLite!")
            return True
        
        print(f"⚠️ O IP {ip} já está cadastrado na farm.")
        return False
    except Exception as e:
        db.session.rollback() # Cancela a operação em caso de erro para não corromper o banco
        print(f"🚨 Falha ao salvar no SQLite: {e}")
        return False
# --- Fim Funcao Salvar Maquina ---

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
    if not os.path.exists(TOKENS_PATH):
        return {}
    try:
        with open(TOKENS_PATH, 'r') as f:
            return json.load(f)
    except:
        return {}

def salvar_tokens(tokens):
    with open(TOKENS_PATH, 'w') as f:
        json.dump(tokens, f, indent=4)




# 2. Lógica de Renovação Automática (OAuth 2.0) - VERSÃO CORRIGIDA
def garantir_token_valido(forcar_renovacao=False):
    tokens = carregar_tokens()
    if not tokens:
        raise Exception("Nenhum token encontrado. Acesse /login_bling primeiro.")

    agora = time.time()
    # ✅ CORREÇÃO: Renova se o tempo expirou OU se o sistema forçar (após erro 401)
    if forcar_renovacao or agora > (tokens.get('expires_at', 0) - 300):
        print("🔄 Renovando acesso para múltiplos dispositivos...")
        url = "https://www.bling.com.br/Api/v3/oauth/token"
        
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": tokens['refresh_token']
        }
        
        # O Bling exige o Client ID e Secret via Basic Auth ou no Payload
        try:
            response = requests.post(url, data=payload, auth=(CLIENT_ID, CLIENT_SECRET), timeout=10)
            
            if response.status_code == 200:
                novos_dados = response.json()
                tokens['access_token'] = novos_dados['access_token']
                # ✅ IMPORTANTE: O Bling pode mandar um novo refresh_token, você deve salvar!
                tokens['refresh_token'] = novos_dados.get('refresh_token', tokens['refresh_token'])
                tokens['expires_at'] = agora + novos_dados['expires_in']
                salvar_tokens(tokens)
                print("✅ Token renovado com sucesso!")
            else:
                # 🚨 Se a renovação falhar, o refresh_token morreu. Precisa de login manual.
                print(f"🚨 Refresh Token expirou ou é inválido: {response.text}")
                return None 
        except Exception as e:
            print(f"🚨 Falha de rede na renovação: {e}")
            return None
    
    return tokens.get('access_token')

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
        token = garantir_token_valido()
        headers = {"Authorization": f"Bearer {token}"}
        url = "https://www.bling.com.br/Api/v3/produtos?estoque=S"
        
        response = requests.get(url, headers=headers, timeout=10)

        # ✅ SEGUNDA CHANCE: Se der 401, força a renovação e tenta de novo
        if response.status_code == 401:
            print("⚠️ Token rejeitado (401). Forçando renovação...")
            novo_token = garantir_token_valido(forcar_renovacao=True)
            if novo_token:
                headers = {"Authorization": f"Bearer {novo_token}"}
                response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Erro no Bling", "details": response.text}), response.status_code

    except Exception as e:
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

# --- 3) Rota Imprimir Interno (Arquivos da Memória da Impressora) ---
@app.route('/api/imprimir_interno', methods=['POST'])
def imprimir_interno():
    """Inicia a impressão de um arquivo que já está na memória da impressora"""
    dados = request.json or {}
    ip = dados.get('ip')
    filename = (dados.get('filename') or '').strip()

    if not ip or not filename:
        return jsonify({"success": False, "message": "IP ou Nome do arquivo ausentes"}), 400

    try:
        # Codifica o nome para garantir que o Klipper entenda espaços no nome
        nome_url = urllib.parse.quote(filename)
        url = f"http://{ip}/printer/print/start?filename={nome_url}"
        
        # Timeout estendido para 15s para dar tempo do hardware responder
        resp = SESSAO_REDE.post(url, timeout=15.0)
        
        if resp.status_code == 200:
            return jsonify({"success": True, "message": f"Impressão de '{filename}' iniciada!"})
        else:
            return jsonify({"success": False, "message": f"Erro na impressora: Status {resp.status_code}"})

    except Exception:
        # Se der timeout mas o sinal sair, retornamos sucesso para não travar a UI
        return jsonify({"success": True, "message": "Comando enviado com sucesso!"})

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

# --- Inicio Bloco de Segurança verificar_ip ---
# --- Inicio Funcao verificar_ip (Versão Final Limpa) ---
def verificar_ip(ip, nome_personalizado):
    """
    Monitora a impressora e atualiza o estado global.
    Gerencia a comunicação com o Klipper e o registro de produção no SQLite.
    """
    # 1. Teste físico de rede (Socket) para evitar travamentos
    if not testar_conexao_rapida(ip):
        FALHAS_CONSECUTIVAS[ip] = FALHAS_CONSECUTIVAS.get(ip, 0) + 1
        if FALHAS_CONSECUTIVAS[ip] >= 2:
            IMPRESSORAS_ENCONTRADAS[ip] = {
                'nome': nome_personalizado, 
                'ip': ip, 
                'status': 'offline', 
                'cor': 'offline', 
                'msg': 'OFFLINE', 
                'progresso': 0, 
                'imagem': 'n4max.png'
            }
        return

    # 2. Busca de telemetria via API do Klipper
    url = f"http://{ip}/printer/objects/query?print_stats&display_status"
    try:
        # Timeout de 3 segundos para tolerar oscilações no Wi-Fi
        resp = SESSAO_REDE.get(url, timeout=3.0)
        
        if resp.status_code == 200:
            FALHAS_CONSECUTIVAS[ip] = 0
            dados = resp.json()['result']['status']
            
            # Extração de dados do Klipper
            status_klipper = dados['print_stats']['state']
            filename = dados['print_stats']['filename']
            progresso = int(dados['display_status']['progress'] * 100)

            # Lógica de Gatilho: Detecta quando a Neptune 4 MAX termina uma peça
            status_anterior = ULTIMO_STATUS_MAQUINAS.get(ip)
            if status_anterior == "printing" and status_klipper == "complete":
                if filename and filename != "Nenhum":
                    registrar_conclusao(filename)
            
            # Atualiza a memória de transição
            ULTIMO_STATUS_MAQUINAS[ip] = status_klipper

            # 3. Mapeamento de Status para o Dashboard
            # Define a mensagem amigável e a cor do card
            if status_klipper == "printing":
                msg_exibicao, cor_status = f"IMPRIMINDO {progresso}%", "printing"
            elif status_klipper in ["startup", "busy"]:
                msg_exibicao, cor_status = "PREPARANDO", "printing"
            elif status_klipper == "paused":
                msg_exibicao, cor_status = "PAUSADO", "paused"
            elif status_klipper in ["standby", "ready", "idle", "complete"]:
                msg_exibicao, cor_status = "PRONTA", "ready"
            else:
                msg_exibicao, cor_status = "OFFLINE", "offline"

            # Atualiza o dicionário global que o JavaScript consome
            IMPRESSORAS_ENCONTRADAS[ip] = {
                'nome': nome_personalizado,
                'ip': ip,
                'status': status_klipper,
                'cor': cor_status,
                'msg': msg_exibicao,
                'progresso': progresso,
                'imagem': "n4max.png",
                'arquivo': filename or "Nenhum"
            }
            
    except Exception:
        # Em caso de erro na API, marca como OFFLINE para não travar em 'CONECTANDO...'
        IMPRESSORAS_ENCONTRADAS[ip] = {
            'nome': nome_personalizado, 
            'ip': ip, 
            'status': 'offline', 
            'cor': 'offline', 
            'msg': 'OFFLINE', 
            'progresso': 0, 
            'imagem': 'n4max.png'
        }
# --- Fim Funcao verificar_ip ---


@app.route('/api/imprimir_biblioteca', methods=['POST'])
def imprimir_biblioteca():
    """Enfileira um arquivo da biblioteca para upload e impressão"""
    dados = request.json or {}
    ip = dados.get('ip')
    arquivo = (dados.get('arquivo') or '').strip().replace("\\", "/").lstrip("/")

    if not ip or not arquivo:
        return jsonify({"success": False, "message": "Dados incompletos"}), 400

    caminho = os.path.abspath(os.path.join(PASTA_RAIZ, arquivo))
    
    if not os.path.exists(caminho):
        return jsonify({"success": False, "message": "Arquivo físico não encontrado"}), 404

    # Envia para a lógica de enfileiramento (Queue) que já funciona no seu app
    enfileirar_impressao(ip, caminho, arquivo_label=os.path.basename(caminho))
    return jsonify({"success": True, "message": "Na fila de transmissão!"})



# --- Monitor Inteligente (Limpo) ---
def monitor_inteligente():
    """Motor principal que mantém a sincronia com o SQLite"""
    while True:
        with app.app_context():
            try:
                maquinas = carregar_maquinas()
                if maquinas:
                    with ThreadPoolExecutor(max_workers=15) as executor:
                        for m in maquinas:
                            executor.submit(
                                lambda p: app.app_context().push() or verificar_ip(p['ip'], p['nome']), 
                                m
                            )
            except Exception:
                pass # Erros silenciosos para não poluir o terminal
        time.sleep(3)

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
    fila = UPLOAD_FILAS[ip]
    while True:
        job = fila.get()
        if job is None: break

        caminho = job.get("caminho")
        nome_exib = job.get("arquivo_label", os.path.basename(caminho))

        # ✅ MELHORIA: Garante que o progresso comece em 1% para o JS detectar atividade
        PROGRESSO_UPLOAD[ip] = {"p": 1, "msg": f"Iniciando: {nome_exib}"}

        try:
            tarefa_upload(ip, caminho)
        except Exception as e:
            PROGRESSO_UPLOAD[ip] = {"p": -1, "msg": f"Erro: {str(e)[:30]}"}
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
        with UPLOAD_SEM:
            # ✅ Status intermediário claro
            PROGRESSO_UPLOAD[ip_alvo] = {"p": 5, "msg": "Conectando à Neptune..."}

            with open(caminho_completo, 'rb') as f:
                monitor = Monitor(f, ip_alvo)
                files = {'file': (nome_arquivo, monitor)}
                
                # Aumentamos o timeout para gcodes pesados de Betim
                resp = SESSAO_REDE.post(
                    f"http://{ip_alvo}/server/files/upload",
                    files=files,
                    timeout=1200 
                )
                resp.raise_for_status()

        PROGRESSO_UPLOAD[ip_alvo] = {"p": 95, "msg": "Processando no Klipper..."}
        
        # Comando de início
        nome_url = urllib.parse.quote(nome_arquivo)
        SESSAO_REDE.post(f"http://{ip_alvo}/printer/print/start?filename={nome_url}", timeout=5)

        # ✅ NÃO APAGUE O STATUS IMEDIATAMENTE
        PROGRESSO_UPLOAD[ip_alvo] = {"p": 100, "msg": "Sucesso!"}
        
    except Exception as e:
        PROGRESSO_UPLOAD[ip_alvo] = {"p": -1, "msg": "Falha no envio"}



# --- Inicio Funcao Registrar Conclusao (Data Corrigida) ---
def registrar_conclusao(nome_arquivo):
    """Registra a peça no SQLite com tratamento moderno de timezone."""
    if not nome_arquivo or nome_arquivo == "Nenhum": 
        return
        
    nome_limpo = nome_arquivo.replace('.gcode', '').replace('.bgcode', '')
    # Uso do timezone.utc para evitar o DeprecationWarning do seu terminal
    hoje = datetime.now(timezone.utc).date()

    try:
        registro = RegistroProducao.query.filter_by(nome_peca=nome_limpo, data=hoje).first()
        if registro:
            registro.quantidade += 1
        else:
            novo_registro = RegistroProducao(nome_peca=nome_limpo, data=hoje, quantidade=1)
            db.session.add(novo_registro)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"🚨 Erro ao registrar produção no SQLite: {e}")
# --- Fim Funcao Registrar Conclusao ---


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

# --- Rota de Status (Limpa) ---
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


# --- 1) Rota Navegar (Acesso a Pastas e Arquivos com Tamanho) ---
# --- Rota Navegar (Blindada e com Metadados) ---
@app.route('/navegar', methods=['POST'])
def navegar():
    try:
        # 1. Validação dos dados recebidos
        dados = request.json or {}
        subpasta = str(dados.get('pasta') or '').strip().replace('\\', '/').strip('/')
        
        # 2. Construção do caminho absoluto
        # Isso evita que o Python se confunda com pastas relativas
        caminho_alvo = os.path.normpath(os.path.join(PASTA_RAIZ, subpasta))
        
        # 3. Trava de Segurança (Não permite sair da pasta empresa/gcodes)
        raiz_abs = os.path.abspath(PASTA_RAIZ)
        alvo_abs = os.path.abspath(caminho_alvo)
        
        if not alvo_abs.startswith(raiz_abs):
            return jsonify({"error": "Acesso Negado: Tentativa de sair da raiz"}), 403

        # 4. Listagem dos itens
        if not os.path.exists(alvo_abs):
            return jsonify({"atual": subpasta, "pastas": [], "arquivos": [], "msg": "Pasta não encontrada"}), 200

        itens = os.listdir(alvo_abs)
        pastas, arquivos = [], []

        for nome in itens:
            if nome.startswith('.') or nome in ['Thumbs.db', '.DS_Store']:
                continue
            
            full_path = os.path.join(alvo_abs, nome)
            
            try:
                if os.path.isdir(full_path):
                    pastas.append({"nome": nome, "tipo": "pasta"})
                else:
                    # Tenta pegar o tamanho do arquivo
                    tamanho_bytes = os.path.getsize(full_path)
                    tamanho_mb = round(tamanho_bytes / (1024 * 1024), 1)
                    arquivos.append({
                        "nome": nome, 
                        "tipo": "arquivo", 
                        "tamanho": f"{tamanho_mb} MB"
                    })
            except Exception as e:
                print(f"⚠️ Erro ao ler item {nome}: {e}")
                continue # Pula arquivos problemáticos

        return jsonify({
            "atual": subpasta,
            "pastas": sorted(pastas, key=lambda x: x['nome']),
            "arquivos": sorted(arquivos, key=lambda x: x['nome'])
        })

    except Exception as e:
        # Se chegar aqui, o erro será exibido no JSON em vez de dar tela de erro 500
        print(f"🚨 Falha na Rota Navegar: {str(e)}")
        return jsonify({"error": f"Erro interno no servidor: {str(e)}"}), 500
    

@app.route('/api/arquivos_internos/<ip>')
def arquivos_internos(ip):
    """Busca a lista de arquivos gcodes salvos dentro da Neptune 4 MAX"""
    try:
        url = f"http://{ip}/server/files/list?root=gcodes"
        
        # Faz a requisição para a impressora
        resp = SESSAO_REDE.get(url, timeout=3.0)
        
        if resp.status_code == 200:
            # 1. Transformamos a resposta em um dicionário Python (JSON)
            dados = resp.json()
            
            # 2. BUSCA CORRETA: Pegamos a lista de arquivos dentro da chave 'result'
            lista_arquivos = dados.get('result', [])
            
            # 3. Ordena: os mais recentes (modified) aparecem primeiro
            arquivos_ordenados = sorted(
                lista_arquivos, 
                key=lambda x: x.get('modified', 0), 
                reverse=True
            )
            return jsonify(arquivos_ordenados)
        else:
            return jsonify({"error": f"Erro {resp.status_code} na impressora"}), resp.status_code

    except Exception as e:
        # Se houver erro de rede ou lógica, retorna 500 com a mensagem do erro
        print(f"🚨 Erro interno na rota arquivos_internos ({ip}): {e}")
        return jsonify({"error": str(e)}), 500

# --- Inicio Rota Remover Impressora ---
@app.route('/remover_impressora', methods=['POST'])
def remover_impressora():
    """
    Remove a máquina do banco de dados SQLite pelo IP.
    """
    ip = request.json.get('ip')
    try:
        maquina = Maquina.query.filter_by(ip=ip).first()
        if maquina:
            db.session.delete(maquina)
            db.session.commit()
            # Limpa da memória de monitoramento em tempo real
            if ip in IMPRESSORAS_ENCONTRADAS:
                del IMPRESSORAS_ENCONTRADAS[ip]
            return jsonify({"success": True})
        return jsonify({"success": False, "message": "Impressora não encontrada"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
# --- Fim Rota Remover Impressora ---

# --- Inicio Funcao Carregar Producao 24h ---
def carregar_producao_24h():
    """
    Busca no banco de dados todas as peças produzidas no dia de hoje.
    Retorna os dados no formato exato que o seu Dashboard (HTML/JS) já utiliza.
    """
    hoje = datetime.now(timezone.utc).date()
    try:
        # Busca todos os registros onde a data é igual a hoje
        registros = RegistroProducao.query.filter_by(data=hoje).all()
        
        # Reconstrói o dicionário de itens para manter a compatibilidade com o seu JavaScript
        itens = {r.nome_peca: r.quantidade for r in registros}
        
        return {
            "data": hoje.strftime("%Y-%m-%d"),
            "itens": itens
        }
    except Exception as e:
        print(f"🚨 Erro ao carregar produção do dia: {e}")
        return {"data": hoje.strftime("%Y-%m-%d"), "itens": {}}
# --- Fim Funcao Carregar Producao 24h ---


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
    # Limpa barras invertidas que podem vir do seletor se rodar em ambiente Windows
    arquivo = (dados.get('arquivo') or '').strip().replace("\\", "/").lstrip("/")

    if not ips or not arquivo:
        return jsonify({"success": False, "message": "ips/arquivo ausentes"}), 400

    caminho = os.path.abspath(os.path.join(PASTA_RAIZ, arquivo))
    if not os.path.exists(caminho):
        return jsonify({"success": False, "message": f"Arquivo não encontrado: {arquivo}"}), 404

    for ip in ips:
        enfileirar_impressao(ip, caminho, arquivo_label=os.path.basename(caminho))

    return jsonify({"success": True, "queued": True, "total": len(ips)})

"""Ações em massa"""


# --- Inicio Bloco de Inicializacao Corrigido ---
if __name__ == '__main__':
    # 1. Criamos a Thread de monitoramento em segundo plano
    t = threading.Thread(target=monitor_inteligente, daemon=True)
    
    # 2. Iniciamos o motor de busca (Sem isso, o status fica em 0)
    t.start()
    print("🚀 Motor de monitoramento iniciado em Betim!")

    # 3. Rodamos o servidor Flask
    app.run(host='0.0.0.0', port=5000, debug=False)
# --- Fim Bloco de Inicializacao ---