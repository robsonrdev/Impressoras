
/* =========================================================
   SUPERTECH FARM CONTROL - SCRIPT PRINCIPAL
   - Organizado por módulos
   - Comentários início/fim
   - Mantém sua lógica original
========================================================= */


/* =========================================================
   0) ESTADO GLOBAL / VARIÁVEIS
========================================================= */
let impressoraSelecionada = '';
let pastaAtual = '';
let uploadsAtivos = {};
let isPollingPaused = false;
let pollingDeep = null;
let nomeImpressoraSelecionada = ''; // Para usar na confirmação de exclusão
/* =========================================================
   /0) ESTADO GLOBAL / VARIÁVEIS
========================================================= */


/* =========================================================
   1) MONITORAMENTO GERAL (GRID + WIDGETS) - VERSÃO LIMPA
   ========================================================= */
function atualizarStatusInstantaneo() {
    if (isPollingPaused) return;

    // 1. Busca o sinal de estado atualizado do servidor Python
    fetch('/status_atualizado')
        .then(r => r.json())
        .then(data => {
            // Atualiza o contador de máquinas prontas no widget superior
            const countDispEl = document.getElementById('countDisp');
            if (countDispEl) {
                countDispEl.innerText = data.total_disponiveis;
            }

            const listaAtiva = document.getElementById('listaProducaoAtiva');
            let htmlListaAtiva = '';
            let temAlguemImprimindo = false;

            // Percorre cada impressora enviada pelo sinal do servidor
            Object.entries(data.impressoras || {}).forEach(([ip, dados]) => {
                // Tenta encontrar o card pelo ID único ou pelo seletor de IP
                const idLimpo = ip.split('.').join('-');
                const card = document.getElementById(`card-${idLimpo}`) || 
                             document.querySelector(`.card-pro[onclick*="${ip}"]`);
                
                if (!card) return; 

                // Ajusta a cor do card (ready, printing, offline) e o texto de status
                card.className = `card-pro ${dados.cor}`;
                const statusTxt = card.querySelector('.status-text');
                if (statusTxt) {
                    statusTxt.innerText = dados.msg;
                }

                // Gerencia a exibição da barra de progresso em tempo real
                const progressArea = card.querySelector('.progress-area');
                if (progressArea) {
                    if (['printing', 'paused'].includes(dados.status)) {
                        progressArea.style.display = 'block';
                        
                        const fill = card.querySelector('.barra-progresso');
                        const pctVal = card.querySelector('.pct-val');
                        
                        if (fill) fill.style.width = dados.progresso + '%';
                        if (pctVal) pctVal.innerText = dados.progresso + '%';
                        
                        // Alimenta a lista da 'Linha de Produção'
                        temAlguemImprimindo = true;
                        const nomeLimpo = (dados.arquivo || '').replace('.gcode', '').replace('.bgcode', '');
                        htmlListaAtiva += `
                            <li>
                                <span class="printer-name">${dados.nome}</span>
                                <span class="file-name">${nomeLimpo}</span>
                            </li>`;
                    } else {
                        progressArea.style.display = 'none';
                    }
                }
            });

            // Renderiza a lista de produção no widget
            if (listaAtiva) {
                listaAtiva.innerHTML = temAlguemImprimindo 
                    ? htmlListaAtiva 
                    : '<li class="empty-msg">Nenhuma colmeia em produção</li>';
            }
        })
        .catch(() => {}); // Falha silenciosa para produção

    // 2. Busca o resumo da produção diária (Concluídos 24h)
    fetch('/dados_producao_diaria')
        .then(r => r.json())
        .then(producao => {
            const listaConcluida = document.getElementById('listaProducaoConcluida');
            if (!listaConcluida) return;

            let htmlConcluido = '';
            Object.entries(producao.itens || {}).forEach(([nome, qtd]) => {
                htmlConcluido += `
                    <li>
                        <span class="printer-name">${nome}</span>
                        <span class="file-name">Qtd: ${qtd}</span>
                    </li>`;
            });
            
            listaConcluida.innerHTML = htmlConcluido || '<li class="empty-msg">Aguardando finalizações...</li>';
        })
        .catch(() => {});
}


/* =========================================================
   2) COMMAND CENTER (TABS / MODAL / DEEP POLLING)
========================================================= */

/**
 * Troca de tabs do Command Center
 * Obs.: agora recebe o event como parâmetro para não depender de variável global
 */
function switchTab(tabId, ev) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

    // Se não vier evento (caso algum lugar chame sem passar), tenta pegar window.event
    const e = ev || window.event;
    if (e && e.target) e.target.classList.add('active');

    const tab = document.getElementById(`tab-${tabId}`);
    if (tab) tab.classList.add('active');
}

/** Confirma exclusão de impressora */
function confirmarExclusaoImpressora() {
    if (!impressoraSelecionada || !nomeImpressoraSelecionada) return;

    const confirmacao = confirm(
        `⚠️ ATENÇÃO, ROBSON!\n\nTem certeza que deseja EXCLUIR a impressora "${nomeImpressoraSelecionada}" (IP: ${impressoraSelecionada})?\n\nEsta ação removerá a máquina permanentemente da sua farm em Betim.`
    );

    if (confirmacao) {
        removerImpressora(impressoraSelecionada, nomeImpressoraSelecionada);
    }
}

function imprimirArquivoBiblioteca(arquivoRelativo) {
    if (!impressoraSelecionada) {
        alert("Selecione uma impressora primeiro.");
        return;
    }

    // Feedback visual de carregamento no botão
    const btn = event.target;
    const textoOriginal = btn.innerText;
    btn.innerText = "ENVIANDO...";
    btn.disabled = true;

    fetch('/api/imprimir_biblioteca', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ip: impressoraSelecionada, arquivo: arquivoRelativo })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            // Sucesso: Fecha a modal para ver o progresso no card
            fecharModal();
        } else {
            alert("❌ Erro: " + res.message);
            btn.innerText = textoOriginal;
            btn.disabled = false;
        }
    })
    .catch(() => {
        alert("🚨 Erro de conexão com o servidor de Betim.");
        btn.innerText = textoOriginal;
        btn.disabled = false;
    });
}

/* =========================================================
   4) ARQUIVOS INTERNOS - INICIAR PRODUÇÃO COM FEEDBACK
   ========================================================= */
function imprimirArquivoInterno(filename){
    if (!impressoraSelecionada) {
        alert("Selecione uma impressora primeiro.");
        return;
    }

    // 1. Feedback visual imediato no botão
    // O 'event.target' identifica qual botão de 'IMPRIMIR' você clicou na lista
    const btn = event.target;
    const textoOriginal = btn.innerText;
    
    btn.innerText = "INICIANDO...";
    btn.disabled = true;
    btn.style.opacity = "0.6";

    fetch('/api/imprimir_interno', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ ip: impressoraSelecionada, filename })
    })
    .then(async r => {
        const res = await r.json();
        if (res.success) {
            // Sucesso real: Mostra alerta e fecha o Command Center
            alert(`✅ Sucesso: ${res.message}`);
            fecharModal();
        } else {
            // Se o Python retornar erro (ex: arquivo corrompido)
            throw new Error(res.message || "Erro no Klipper");
        }
    })
    .catch(err => {
        // Trata erros de rede ou timeout (comum em Betim)
        alert("❌ Erro: " + err.message);
        
        // Restaura o botão caso dê erro para você tentar de novo
        btn.innerText = textoOriginal;
        btn.disabled = false;
        btn.style.opacity = "1";
    });
}


/** Remove impressora no backend */
function removerImpressora(ip, nome) {
    fetch('/remover_impressora', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ip: ip })
    })
    .then(response => {
        if (response.ok) {
            fecharModal();
            window.location.reload();
        } else {
            alert("Erro ao remover a impressora. Verifique a conexão com o servidor.");
        }
    })
    .catch(err => console.error("Erro na exclusão:", err));
}

/** Deep polling (detalhes profundos do Klipper) */
function deepPollingCC() {
    if (!impressoraSelecionada) return;

    fetch(`/api/detalhes_profundos/${impressoraSelecionada}`)
        .then(r => r.json())
        .then(data => {
            // Atualiza temperaturas (com validação)
            if (data.status && data.status.extruder && data.status.heater_bed) {
                const extCur = document.getElementById('temp-ext-cur');
                const extTar = document.getElementById('temp-ext-tar');
                const bedCur = document.getElementById('temp-bed-cur');
                const bedTar = document.getElementById('temp-bed-tar');

                if (extCur) extCur.innerText = Math.round(data.status.extruder.temperature) || 0;
                if (extTar) extTar.innerText = Math.round(data.status.extruder.target) || 0;
                if (bedCur) bedCur.innerText = Math.round(data.status.heater_bed.temperature) || 0;
                if (bedTar) bedTar.innerText = Math.round(data.status.heater_bed.target) || 0;
            }

            // Atualiza console
            const log = document.getElementById('cc-console-log');
            if (data.console && log) {
                log.innerHTML = data.console
                    .map(line => `<div class="console-line"><span>></span> ${line.message}</div>`)
                    .join('');
                log.scrollTop = log.scrollHeight;
            }
        })
        .catch(err => console.error("Erro no Deep Polling:", err));
}

/** Fecha modal de cadastro (resolve bug tablet) */
function fecharModalCadastro() {
    const modal = document.getElementById('modalCadastro');
    if (modal) modal.style.display = "none";
}

/** Abre modal de cadastro com fundo forçado */
function abrirModalCadastro() {
    fecharModal(); // fecha o CC
    const modal = document.getElementById('modalCadastro');
    if (!modal) return;

    modal.style.display = "flex";
    modal.style.backgroundColor = "rgba(0, 0, 0, 0.9)";
    modal.style.backdropFilter = "blur(15px)";
}

/** Abre Command Center */
function abrirModal(ip, nome) {
    fecharModalCadastro();

    impressoraSelecionada = ip;
    nomeImpressoraSelecionada = nome;

    const modal = document.getElementById('modalImprimir');
    if (modal) modal.style.display = "flex";

    const titulo = document.getElementById('tituloModal');
    const badge = document.getElementById('cc-ip-badge');
    if (titulo) titulo.innerText = nome;
    if (badge) badge.innerText = ip;

    const full = document.getElementById('fullScreenSuccess');
    if (full) full.classList.remove('show');

    // Reset para monitor
    switchTab('monitor');

    // Carrega arquivos e inicia polling
    carregarPasta('');
    carregarArquivosInternos();

    if (pollingDeep) clearInterval(pollingDeep);
    pollingDeep = setInterval(deepPollingCC, 2000);
}

/** Fecha Command Center */
function fecharModal() {
    const modal = document.getElementById('modalImprimir');
    if (modal) modal.style.display = "none";

    if (pollingDeep) clearInterval(pollingDeep);
    pollingDeep = null;

    impressoraSelecionada = '';
}
/* =========================================================
   /2) COMMAND CENTER (TABS / MODAL / DEEP POLLING)
========================================================= */


/* =========================================================
   3) ARQUIVOS (BIBLIOTECA CENTRAL / NAVEGAÇÃO / SELEÇÃO)
========================================================= */
/* =========================================================
   3) BIBLIOTECA CENTRAL - NAVEGAÇÃO E ESTILIZAÇÃO UNIFICADA
   ========================================================= */
function carregarPasta(caminho) {
    const ul = document.getElementById('listaGcodes');
    if (!ul) return;

    ul.innerHTML = '<li class="loading-state">Acessando arquivos do servidor...</li>';

    fetch('/navegar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pasta: caminho })
    })
    .then(r => r.json())
    .then(dados => {
        pastaAtual = dados.atual || '';
        ul.innerHTML = '';

        const caminhoEl = document.getElementById('caminhoAtual');
        if (caminhoEl) caminhoEl.innerText = pastaAtual || 'Raiz';

        const btnVoltar = document.getElementById('btnVoltar');
        if (btnVoltar) btnVoltar.disabled = (pastaAtual === '');

        // Renderiza Pastas (Estilo Memória Interna)
        dados.pastas.forEach(p => {
            const li = document.createElement('li');
            li.className = 'internal-file-item';
            li.innerHTML = `
                <div class="file-info">
                    <strong class="file-name-text">📁 ${p.nome}</strong>
                    <small class="file-size-tag">Diretório</small>
                </div>
                <button class="btn-print-internal" onclick="carregarPasta('${pastaAtual ? pastaAtual + '/' + p.nome : p.nome}')">ABRIR</button>
            `;
            ul.appendChild(li);
        });

        // Renderiza Arquivos (Estilo Memória Interna com Trava de Extensão)
        dados.arquivos.forEach(f => {
            const isGcode = f.nome.toLowerCase().endsWith('.gcode') || f.nome.toLowerCase().endsWith('.bgcode');
            const rel = pastaAtual ? `${pastaAtual}/${f.nome}` : f.nome;
            
            const li = document.createElement('li');
            li.className = 'internal-file-item';
            li.innerHTML = `
                <div class="file-info">
                    <strong class="file-name-text">${f.nome}</strong>
                    <small class="file-size-tag">${f.tamanho} MB</small>
                </div>
                <button class="btn-print-internal" 
                        ${isGcode ? '' : 'disabled style="opacity: 0.3; cursor: not-allowed;"'}
                        onclick="imprimirArquivoBiblioteca('${rel}')">
                    ${isGcode ? 'IMPRIMIR' : 'SÓ GCODE'}
                </button>
            `;
            ul.appendChild(li);
        });

        if (dados.pastas.length === 0 && dados.arquivos.length === 0) {
            ul.innerHTML = '<li class="empty-msg">Pasta vazia</li>';
        }
    })
    .catch(() => {
        ul.innerHTML = '<li class="error-msg">Erro ao carregar arquivos do servidor.</li>';
    });
}


function selecionarArquivo(el, nome) {
    document.querySelectorAll('.item-file').forEach(i => i.classList.remove('selected'));
    el.classList.add('selected');

    const btn = document.getElementById('btnEnviar');
    if (!btn) return;

    btn.disabled = false;
    btn.dataset.arquivo = pastaAtual + (pastaAtual ? '\\' : '') + nome;
    btn.innerText = "INICIAR PRODUÇÃO";
}

function voltarPasta() {
    let partes = (pastaAtual || '').split('\\');
    partes.pop();
    carregarPasta(partes.join('\\'));
}
/* =========================================================
   /3) ARQUIVOS (BIBLIOTECA CENTRAL / NAVEGAÇÃO / SELEÇÃO)
========================================================= */


/* =========================================================
   4) ARQUIVOS INTERNOS (KLIPPER)
========================================================= */
function carregarArquivosInternos() {
    const lista = document.getElementById('listaArquivosInternos');
    if (!lista) return;

    lista.innerHTML = '<li class="loading-state">Lendo memória da impressora...</li>';

    fetch(`/api/arquivos_internos/${impressoraSelecionada}`)
        .then(r => r.json())
        .then(arquivos => {
            if (!arquivos || arquivos.length === 0) {
                lista.innerHTML = '<li class="empty-msg">Nenhum arquivo na memória.</li>';
                return;
            }

            lista.innerHTML = arquivos.map(f => {
                const nomeArquivo = f.path || f.name || "Arquivo s/ nome";
                const tamanhoMB = (f.size / 1024 / 1024).toFixed(1);

                return `
                    <li class="internal-file-item">
                        <div class="file-info">
                            <strong class="file-name-text">${nomeArquivo}</strong>
                            <small class="file-size-tag">${tamanhoMB} MB</small>
                        </div>
                        <button class="btn-print-internal"
                             onclick="imprimirArquivoInterno('${nomeArquivo.replace(/'/g, "\\'")}')">
                         IMPRIMIR
                    </button>

                    </li>
                `;
            }).join('');
        })
        .catch(err => {
            console.error("Erro ao ler Klipper:", err);
            lista.innerHTML = '<li class="error-msg">Erro ao conectar com a impressora.</li>';
        });
}
/* =========================================================
   /4) ARQUIVOS INTERNOS (KLIPPER)
========================================================= */


/* =========================================================
   5) UPLOAD / TRANSMISSÃO (RECUPERAÇÃO + MONITORAMENTO)
========================================================= */

/** Reconecta aos uploads ativos ao carregar a página */
function recuperarEstadoUploads() {
    const cards = document.querySelectorAll('.card-pro');

    cards.forEach(card => {
        const onclick = card.getAttribute('onclick') || '';
        const ipMatch = onclick.match(/'([^']+)'/);
        if (!ipMatch) return;

        const ip = ipMatch[1];
        const idLimpo = ip.split('.').join('-');

        fetch(`/progresso_transmissao/${ip}`)
            .then(r => r.json())
            .then(d => {
                if (d.p > 0 && d.p < 100) {
                    const loader = document.getElementById(`loader-${idLimpo}`);
                    if (loader) loader.style.display = 'flex';
                    iniciarMonitoramentoUpload(ip, idLimpo);
                }
            });
    });
}

/** Isola monitoramento para reutilizar */
function iniciarMonitoramentoUpload(ip, idLimpo) {
    if (uploadsAtivos[ip]) clearInterval(uploadsAtivos[ip]);

    uploadsAtivos[ip] = setInterval(() => {
        fetch(`/progresso_transmissao/${ip}`)
            .then(r => r.json())
            .then(d => {
                const fill = document.getElementById(`fill-${idLimpo}`);
                const pct = document.getElementById(`pct-${idLimpo}`);

                if (fill) fill.style.width = d.p + '%';
                if (pct) pct.innerText = d.p + '%';

                if (d.p >= 100) {
                    clearInterval(uploadsAtivos[ip]);
                    finalizarVisualUpload(idLimpo);
                }
            });
    }, 800);
}

function finalizarVisualUpload(idLimpo) {
    const content = document.getElementById(`content-${idLimpo}`);
    const success = document.getElementById(`success-${idLimpo}`);
    const loader = document.getElementById(`loader-${idLimpo}`);

    if (content) content.style.display = 'none';
    if (success) success.style.display = 'flex';

    setTimeout(() => {
        if (loader) loader.style.display = 'none';
    }, 3000);
}
/* =========================================================
   /5) UPLOAD / TRANSMISSÃO (RECUPERAÇÃO + MONITORAMENTO)
========================================================= */


/* =========================================================
   6) AÇÕES: CADASTRO / ENVIO / COMANDOS
========================================================= */

/** Cadastro de nova impressora */
function cadastrarNovaImpressora() {
    const nome = (document.getElementById('nomeImpressora') || {}).value;
    const ip = (document.getElementById('novoIpImpressora') || {}).value;

    if (!nome || !ip) {
        alert("⚠️ Por favor, preencha o Nome e o IP da impressora.");
        return;
    }

    console.log(`Enviando cadastro: ${nome} - ${ip}`);

    fetch('/cadastrar_impressora', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nome: nome, ip: ip })
    })
    .then(response => {
        if (response.ok) {
            fecharModalCadastro();
            window.location.reload();
        } else {
            alert("❌ Erro ao cadastrar. Verifique se o IP já existe ou a conexão com o servidor.");
        }
    })
    .catch(err => {
        console.error("Erro na requisição de cadastro:", err);
        alert("🚨 Falha crítica ao conectar com o servidor de Betim.");
    });
}

/** Envia comando gcode / ação */
function enviarComandoCC(cmd, params = '') {
    fetch('/api/comando_gcode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ ip: impressoraSelecionada, comando: cmd, extra: params })
    });
}

/** Clique no botão de enviar (produção) - ligado quando DOM estiver pronto */
function configurarBotaoEnviar() {
    const btn = document.getElementById('btnEnviar');
    if (!btn) return;

    btn.onclick = function() {
        const arquivo = this.dataset.arquivo;
        const ip = impressoraSelecionada;

        if (!arquivo || !ip) {
            alert("⚠️ Selecione um arquivo e uma impressora antes de iniciar.");
            return;
        }

        const idLimpo = ip.split('.').join('-');

        fecharModal();

        // Mostra loader do card
        const loader = document.getElementById(`loader-${idLimpo}`);
        if (loader) loader.style.display = 'flex';

        // Dispara impressão
        fetch('/imprimir', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ ip: ip, arquivo: arquivo })
        });

        // Reutiliza monitoramento isolado
        iniciarMonitoramentoUpload(ip, idLimpo);
    };
}
/* =========================================================
   /6) AÇÕES: CADASTRO / ENVIO / COMANDOS
========================================================= */


/* =========================================================
   7) CONTROLES (STATUS DRAWER / TEMPERATURA / MOVIMENTO / EMERGÊNCIA)
========================================================= */
function toggleStatusDrawer() {
    const drawer = document.querySelector('.summary-widgets');
    if (drawer) drawer.classList.toggle('drawer-open');

    const overlay = document.querySelector('.sidebar-overlay');
    if (overlay) overlay.classList.toggle('active');
}

/** Temperatura interativa */
function configurarTemperatura(tipo) {
    const card = document.querySelector(`.card-pro[onclick*="${impressoraSelecionada}"]`);
    if (!card) return;

    const statusAtual = card.className;

    if (statusAtual.includes('printing')) {
        alert("⚠️ Operação bloqueada! Não é possível alterar a temperatura durante a impressão.");
        return;
    }

    const valor = prompt(`Definir temperatura para ${tipo === 'extruder' ? 'Bico' : 'Mesa'} (°C):`);
    if (valor && !isNaN(valor)) {
        const cmd = tipo === 'extruder' ? `M104 S${valor}` : `M140 S${valor}`;
        enviarComandoCC(cmd);
    }
}

/** Movimentação relativa */
function moverImpressora(eixo, distancia) {
    const card = document.querySelector(`.card-pro[onclick*="${impressoraSelecionada}"]`);
    if (!card) return;

    const statusAtivo = card.className;
    if (statusAtivo.includes('printing')) return;

    const gcode = `G91\nG1 ${eixo}${distancia} F3000\nG90`;
    enviarComandoCC(gcode);
}

/** Parada de emergência */
function paradaDeEmergencia(ip, nome) {
    const confirmar = confirm(
        `🚨 PARADA DE EMERGÊNCIA!\n\nTem certeza que deseja BLOQUEAR IMEDIATAMENTE a impressora "${nome}"?\n\nIsso cancelará a impressão e desligará os motores.`
    );

    if (!confirmar) return;

    console.log(`!!! DISPARANDO M112 PARA ${ip} !!!`);

    fetch('/api/comando_gcode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ip: ip, comando: 'M112' })
    })
    .then(response => {
        if (response.ok) {
            alert("🔴 Comando de Emergência Enviado! A máquina foi interrompida.");
            window.location.reload();
        }
    });
}
/* =========================================================
   /7) CONTROLES (STATUS DRAWER / TEMPERATURA / MOVIMENTO / EMERGÊNCIA)
========================================================= */


/* =========================================================
   8) NAVEGAÇÃO DE TELAS (DASHBOARD / ESTOQUE)
========================================================= */
function trocarTela(tela) {
    const dashboard = document.getElementById('secao-dashboard');
    const estoque = document.getElementById('secao-estoque');
    const titulo = document.getElementById('titulo-pagina');
    const subtitulo = document.getElementById('subtitulo-pagina');

    // Remove 'active' de todos
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));

    if (tela === 'dashboard') {
        if (dashboard) dashboard.style.display = 'block';
        if (estoque) estoque.style.display = 'none';

        const linkDash = document.getElementById('link-dashboard');
        if (linkDash) linkDash.classList.add('active');

        if (titulo) titulo.innerText = "Olá, Robson!";
        if (subtitulo) subtitulo.innerHTML = "Monitoramento de Farm - <strong>Betim</strong>";

    } else if (tela === 'estoque') {
        if (dashboard) dashboard.style.display = 'none';
        if (estoque) estoque.style.display = 'block';

        const linkEstoque = document.getElementById('link-estoque');
        if (linkEstoque) linkEstoque.classList.add('active');

        if (titulo) titulo.innerText = "Gestão de Estoque";
        if (subtitulo) subtitulo.innerHTML = "Sincronização Ativa - <strong>Mercado Livre & Shopee</strong>";

        carregarEstoqueBling();
    }
}
/* =========================================================
   /8) NAVEGAÇÃO DE TELAS (DASHBOARD / ESTOQUE)
========================================================= */

function toggleSidebar(force) {
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.querySelector('.sidebar-overlay');
  const btn = document.getElementById('btnHamburger');

  if (!sidebar || !overlay) return;

  const shouldOpen = (typeof force === 'boolean')
    ? force
    : !sidebar.classList.contains('is-open');

  sidebar.classList.toggle('is-open', shouldOpen);
  overlay.classList.toggle('active', shouldOpen);

  if (btn) btn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
}

// Fecha o menu quando clicar em algum item do menu (boa UX)
document.addEventListener('click', (e) => {
  const isMobile = window.matchMedia('(max-width: 1024px)').matches;
  if (!isMobile) return;

  if (e.target.closest('.sidebar-nav .nav-item')) {
    toggleSidebar(false);
  }
});

// Botão hambúrguer
document.addEventListener('DOMContentLoaded', () => {
    const overlay = document.querySelector('.sidebar-overlay');
if (overlay) overlay.addEventListener('click', () => toggleSidebar(false));
  const btn = document.getElementById('btnHamburger');
  if (btn) btn.addEventListener('click', () => toggleSidebar());
});

// Segurança: se sair do modo responsivo, fecha e remove overlay
window.addEventListener('resize', () => {
  const isMobile = window.matchMedia('(max-width: 1024px)').matches;
  if (!isMobile) toggleSidebar(false);
});



/* =========================================================
   9) ESTOQUE (BLING) + AJUSTES
========================================================= */
function carregarEstoqueBling() {
    const grid = document.getElementById('gridEstoque');
    if (!grid) return;

    grid.innerHTML = '<p class="loading">Sincronizando Farm SuperTech...</p>';

    fetch('/api/estoque_bling')
        .then(res => res.json())
        .then(res => {
            grid.innerHTML = '';
            const produtos = res.data || [];

            produtos.forEach(prod => {
                const saldo = (prod.estoque && prod.estoque.saldoVirtualTotal !== undefined)
                    ? Math.floor(prod.estoque.saldoVirtualTotal)
                    : 0;

                const nome = prod.nome || "Colmeia Climatizador";
                const sku = prod.codigo || "S/ SKU";
                const foto = prod.imagemURL || '/static/img/n4max.png';

                grid.innerHTML += `
                    <div class="product-card">
                        <img src="${foto}" class="product-image" onerror="this.src='/static/img/n4max.png'">

                        <div class="product-info">
                            <h3>${nome}</h3>
                            <span class="sku-tag">SKU: ${sku}</span>
                        </div>

                        <div class="stock-status">
                            <div class="stock-count">
                                ${saldo}<span>un</span>
                            </div>
                            <button class="btn-adjust" onclick="abrirModalAjuste('${prod.id}', '${nome}')">
                                Ajustar
                            </button>
                        </div>
                    </div>
                `;
            });
        });
}

/** Prompt simples de ajuste (tablet/mobile) */
function abrirModalAjuste(id, nome) {
    const qtd = prompt(`📦 ENTRADA DE ESTOQUE\nProduto: ${nome}\n\nQuantas unidades foram produzidas agora em Betim?`);

    if (qtd !== null && !isNaN(qtd) && qtd > 0) {
        ajustarEstoqueManual(id, parseInt(qtd));
    }
}

/** Render alternativo (mantido) */
function renderizarProdutos(dados) {
    const grid = document.getElementById('gridEstoque');
    if (!grid) return;

    grid.innerHTML = '';

    (dados.data || []).forEach(prod => {
        let saldo = 0;
        if (prod.estoque && prod.estoque.saldoVirtual !== undefined) {
            saldo = Math.floor(prod.estoque.saldoVirtual);
        }

        const foto = prod.imagemURL || '/static/img/n4max.png';
        const sku = prod.codigo || "S/ SKU";

        grid.innerHTML += `
            <div class="product-card">
                <img src="${foto}" class="product-image" onerror="this.src='/static/img/n4max.png'">
                <div class="product-info">
                    <h3>${prod.nome}</h3>
                    <span class="sku-tag">SKU: ${sku}</span>
                </div>
                <div class="stock-status">
                    <div class="stock-count">${saldo}<span>un</span></div>
                    <button class="btn-adjust" onclick="abrirModalAjuste('${prod.id}', '${prod.nome}')">Ajustar</button>
                </div>
            </div>
        `;
    });
}

/** Envia ajuste para o backend */
function ajustarEstoqueManual(produtoId, novaQtd) {
    fetch('/api/adicionar_estoque', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: produtoId, quantidade: novaQtd })
    })
    .then(res => res.json())
    .then(data => {
        if (data.error) {
            alert("❌ Erro ao atualizar: " + data.error);
        } else {
            alert(`✅ Sucesso! +${novaQtd} unidades enviadas para Mercado Livre/Shopee.`);
            carregarEstoqueBling();
        }
    })
    .catch(err => alert("🚨 Erro crítico de rede: " + err));
}
/* =========================================================
   /9) ESTOQUE (BLING) + AJUSTES
========================================================= */
/* ===========================
   AÇÃO EM MASSA (VERSÃO MODAL)
   Compatível com index.html + app.py
=========================== */

let selectedPrinters = new Set();

/* sincroniza um checkbox individual */
function syncSelectionFromCheckbox(cb){
  const ip = cb.dataset.ip;
  if (!ip) return;

  if (cb.checked) selectedPrinters.add(ip);
  else selectedPrinters.delete(ip);

  // feedback visual no card (opcional)
  const card = document.querySelector(`.card-pro[onclick*="${ip}"]`);
  if (card) card.style.outline = cb.checked ? "2px solid rgba(255,109,0,0.7)" : "none";
}

/* retorna IPs selecionados */
function getSelectedIps(){
  return Array.from(selectedPrinters);
}

/* chamado no onclick/onchange do checkbox */
function atualizarCountMassa(){
  document.querySelectorAll('.mass-check').forEach(cb => syncSelectionFromCheckbox(cb));

  const countEl = document.getElementById('massaCount');
  if (countEl) countEl.textContent = `${selectedPrinters.size} selecionadas`;
}

/* ===========================
   MODAL
=========================== */
function abrirModalMassa(){
  atualizarCountMassa();
  const modal = document.getElementById('modalMassa');
  if (modal) modal.style.display = 'flex';
  renderMassaParams();
}

function fecharModalMassa(){
  const modal = document.getElementById('modalMassa');
  if (modal) modal.style.display = 'none';
}

/* ===========================
   SELEÇÃO EM MASSA
   - selecionarTodas(true)  -> marca
   - selecionarTodas(false) -> desmarca
   - selecionarTodas()      -> TOGGLE (se todas marcadas, limpa; senão marca)
=========================== */
function selecionarTodas(flag){
  const all = document.querySelectorAll('.mass-check');
  if (!all.length) return;

  // ✅ TOGGLE quando vier sem parâmetro (caso da toolbar)
  if (typeof flag !== 'boolean') {
    const todasMarcadas = Array.from(all).every(cb => cb.checked);
    flag = !todasMarcadas; // se todas marcadas -> false (limpa); se não -> true (marca)
  }

  all.forEach(cb => {
    cb.checked = flag;

    const ip = cb.dataset.ip;
    if (!ip) return;

    if (flag) selectedPrinters.add(ip);
    else selectedPrinters.delete(ip);

    const card = document.querySelector(`.card-pro[onclick*="${ip}"]`);
    if (card) card.style.outline = flag ? "2px solid rgba(255,109,0,0.7)" : "none";
  });

  atualizarCountMassa();
}

/* ===========================
   PARAMETROS POR AÇÃO
=========================== */
document.addEventListener('DOMContentLoaded', () => {
  const sel = document.getElementById('massaAcao');
  if (sel) sel.addEventListener('change', renderMassaParams);
});

function renderMassaParams(){
  const acao = document.getElementById('massaAcao')?.value;
  const box = document.getElementById('massaParams');
  if (!box) return;

  if (acao === "PRINT_FILE"){
    box.innerHTML = `
      <label style="display:block; margin:10px 0 6px;">Arquivo (relativo à /app/gcodes)</label>
      <input id="massaArquivo" type="text"
             placeholder="Ex: pasta1/arquivo.gcode"
             style="width:100%; padding:12px; border-radius:12px;">
    `;
    return;
  }

  if (acao === "SET_TEMP"){
    box.innerHTML = `
      <label style="display:block; margin:10px 0 6px;">Aquecer</label>
      <select id="massaTipoTemp" style="width:100%; padding:12px; border-radius:12px;">
        <option value="bico">Bico (M104)</option>
        <option value="mesa">Mesa (M140)</option>
      </select>

      <label style="display:block; margin:10px 0 6px;">Temperatura (°C)</label>
      <input id="massaTemp" type="number"
             placeholder="Ex: 200"
             style="width:100%; padding:12px; border-radius:12px;">
    `;
    return;
  }

  box.innerHTML = `<p style="opacity:.75; margin-top:10px;">Sem parâmetros adicionais.</p>`;
}

/* ===========================
   EXECUÇÃO EM MASSA
=========================== */
function executarAcaoMassa(){
  const ips = getSelectedIps();
  if (ips.length === 0) return alert("Selecione pelo menos 1 impressora.");

  const acao = document.getElementById('massaAcao')?.value;

  if (acao === "PRINT_FILE"){
    const arquivo = document.getElementById('massaArquivo')?.value?.trim();
    if (!arquivo) return alert("Informe o arquivo.");

    if (!confirm(`Enviar "${arquivo}" para ${ips.length} impressoras?`)) return;

    fetch('/imprimir_em_massa', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({ ips, arquivo })
})
.then(async r => {
  const text = await r.text();
  let json;
  try { json = JSON.parse(text); } catch { json = null; }

  if (!r.ok) {
    throw new Error(json?.message || text || `HTTP ${r.status}`);
  }
  return json;
})
.then(res => {
  alert(`✅ Enfileirado para ${res.total || ips.length} impressoras!`);
  fecharModalMassa();
})
.catch(err => alert("❌ Falha no envio em massa: " + err.message));


    return;
  }

  let comando = "";

  if (acao === "SET_TEMP"){
    const tipo = document.getElementById('massaTipoTemp')?.value;
    const temp = document.getElementById('massaTemp')?.value;
    if (!temp || isNaN(temp)) return alert("Temperatura inválida.");

    comando = (tipo === "mesa") ? `M140 S${temp}` : `M104 S${temp}`;
  }
  else if (acao === "HOME_ALL"){
    comando = "G28";
  }
  else if (acao === "PAUSE" || acao === "RESUME" || acao === "CANCEL"){
    comando = acao;
  }
  else {
    return alert("Ação inválida.");
  }

  fetch('/api/comando_gcode_em_massa', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ ips, comando })
  })
  .then(r => r.json())
  .then(res => {
    if (res.success) alert(`✅ Comando enviado! (${res.ok || 0}/${res.total || ips.length})`);
    else alert("❌ Falha: " + (res.message || "erro"));
    fecharModalMassa();
  })
  .catch(err => alert("🚨 Erro de rede: " + err));
}

/* ===========================
   /AÇÃO EM MASSA (VERSÃO MODAL)
=========================== */


/* =========================================================
   10) BOOTSTRAP (INIT)
========================================================= */
function initDashboard() {
    // 1) reconecta uploads pendentes
    recuperarEstadoUploads();

    // 2) liga botão "Enviar" com segurança após DOM
    configurarBotaoEnviar();

    // 3) inicia polling do dashboard
    setInterval(atualizarStatusInstantaneo, 3000);
    atualizarStatusInstantaneo();
}

window.addEventListener('load', initDashboard);
/* =========================================================
   /10) BOOTSTRAP (INIT)
========================================================= */
