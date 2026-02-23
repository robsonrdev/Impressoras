
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
let passoAtual = 1;
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

    // ativa o conteúdo
    const tab = document.getElementById(`tab-${tabId}`);
    if (tab) tab.classList.add('active');

    // ativa o botão (com ou sem evento)
    let btn = null;

    const e = ev || window.event;
    if (e && e.target) {
        btn = e.target.closest('.tab-btn');
    }

    if (!btn) {
        // tenta encontrar o botão da aba pelo atributo data-tab (recomendado no HTML)
        btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    }

    if (!btn) {
        // fallback: procura pelo onclick que contenha o tabId
        btn = Array.from(document.querySelectorAll('.tab-btn'))
            .find(b => (b.getAttribute('onclick') || '').includes(`'${tabId}'`) || (b.getAttribute('onclick') || '').includes(`"${tabId}"`));
    }

    if (btn) btn.classList.add('active');
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
  if (!impressoraSelecionada) return alert("Selecione uma impressora primeiro.");

  // 🛡️ trava os dados antes de fechar modal / trocar estado
  const ipAlvo = impressoraSelecionada;
  const nomeAlvo = nomeImpressoraSelecionada;
  const idLimpo = ipAlvo.split('.').join('-');

  if (!confirm(`🚀 INICIAR PRODUÇÃO?\n\nArquivo: ${arquivoRelativo}\nDestino: ${nomeAlvo}`)) return;

  const loader = document.getElementById(`loader-${idLimpo}`);
  if (loader) loader.style.display = 'flex';

  fetch('/imprimir', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ip: ipAlvo, arquivo: arquivoRelativo })
  })
  .then(async (r) => {
    const txt = await r.text().catch(() => '');
    let payload = null;
    try { payload = JSON.parse(txt); } catch {}

    // ✅ Se HTTP falhou, mostra o retorno real
    if (!r.ok) {
      console.error("Resposta /imprimir (HTTP):", r.status, txt);
      throw new Error(payload?.message || `Erro HTTP: ${r.status}`);
    }

    // ✅ Se veio 200 mas success false, também trata como erro
    if (payload && payload.success === false) {
      console.error("Resposta /imprimir (success=false):", payload);
      throw new Error(payload.message || "Falha ao iniciar impressão no servidor.");
    }

    return payload || { success: true };
  })
  .then(() => {
    fecharModal();
    iniciarMonitoramentoUpload(ipAlvo, idLimpo);
  })
  .catch(err => {
    console.error("🚨 Falha na transmissão:", err);
    alert(`❌ Erro ao enviar arquivo.\n\nDetalhe: ${err.message}`);
    if (loader) loader.style.display = 'none';
  });
}
/* =========================================================
   4) ARQUIVOS INTERNOS - INICIAR PRODUÇÃO COM FEEDBACK
   ========================================================= */
   
async function imprimirArquivoInterno(filename, event) {
    if (!impressoraSelecionada) return alert("Selecione a máquina.");

    const config = {
        ip: impressoraSelecionada,
        nome: nomeImpressoraSelecionada,
        id: impressoraSelecionada.split('.').join('-')
    };

    if (!confirm(`Confirmar início de: ${filename}?`)) return;

    const btn = (event && event.target) ? event.target.closest('button') : null;
    const textoOriginal = btn ? btn.innerHTML : null;

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner"></span> PROCESSANDO...`;
    }

    try {
        const response = await fetch('/api/imprimir_interno', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ip: config.ip, filename: filename })
        });

        const result = await response.json();

        if (!result.success) throw new Error(result.message || "Falha ao iniciar impressão");

        fecharModal();

        // Aqui o upload não acontece (arquivo já está interno),
        // mas você quis manter feedback padrão, então mantemos:
        iniciarMonitoramentoUpload(config.ip, config.id);

    } catch (error) {
        console.error("🚨 Erro na Farm:", error);
        alert(`Erro: ${error.message}`);
        if (btn && textoOriginal) btn.innerHTML = textoOriginal;
    } finally {
        if (btn) btn.disabled = false;
    }
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
   3) BIBLIOTECA CENTRAL - NAVEGAÇÃO COM ESTILO UNIFICADO
   ========================================================= */
function carregarPasta(caminho) {
    const ul = document.getElementById('listaGcodes');
    if (!ul) return;

    ul.innerHTML = '<li class="loading-state">Lendo arquivos do servidor...</li>';

    fetch('/navegar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pasta: caminho })
    })
    .then(r => r.json())
    .then(dados => {
        // Se o servidor retornar erro no JSON, exibe na tela
        if (dados.error) {
            ul.innerHTML = `<li class="error-msg">⚠️ ${dados.error}</li>`;
            return;
        }

        pastaAtual = dados.atual || '';
        ul.innerHTML = '';

        const caminhoEl = document.getElementById('caminhoAtual');
        if (caminhoEl) caminhoEl.innerText = pastaAtual || 'Raiz';

        const btnVoltar = document.getElementById('btnVoltar');
        if (btnVoltar) btnVoltar.disabled = (pastaAtual === '');

        // 1. Renderiza Pastas (Estilo Klipper)
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

        // 2. Renderiza Arquivos (Com tag de MB laranja)
        dados.arquivos.forEach(f => {
            const isGcode = f.nome.toLowerCase().endsWith('.gcode') || f.nome.toLowerCase().endsWith('.bgcode');
            const rel = pastaAtual ? `${pastaAtual}/${f.nome}` : f.nome;
            
            const li = document.createElement('li');
            li.className = 'internal-file-item';
            li.innerHTML = `
                <div class="file-info">
                    <strong class="file-name-text">${f.nome}</strong>
                    <small class="file-size-tag">${f.tamanho}</small>
                </div>
                <button class="btn-print-internal" 
                        ${isGcode ? '' : 'disabled style="opacity: 0.3;"'}
                        onclick="imprimirArquivoBiblioteca('${rel}')">
                    ${isGcode ? 'IMPRIMIR' : 'SÓ GCODE'}
                </button>
            `;
            ul.appendChild(li);
        });

        if (dados.pastas.length === 0 && dados.arquivos.length === 0) {
            ul.innerHTML = '<li class="empty-msg">Nenhum arquivo nesta pasta.</li>';
        }
    })
    .catch(err => {
        ul.innerHTML = '<li class="error-msg">Erro de conexão com o servidor.</li>';
    });
}

/* No módulo 3 (ARQUIVOS) */
function selecionarArquivo(el, nome) {
    document.querySelectorAll('.item-file').forEach(i => i.classList.remove('selected'));
    el.classList.add('selected');

    const btn = document.getElementById('btnEnviar');
    if (!btn) return;

    btn.disabled = false;
    // ✅ Padronizado para barra normal (Linux/Samba)
    btn.dataset.arquivo = pastaAtual + (pastaAtual ? '/' : '') + nome;
    btn.innerText = "INICIAR PRODUÇÃO";
}

function voltarPasta() {
    // ✅ Padronizado para barra normal
    let partes = (pastaAtual || '').split('/');
    partes.pop();
    carregarPasta(partes.join('/'));
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
                const nomeArquivo = (f.path || f.name || "Arquivo s/ nome");
                const tamanhoMB = (typeof f.size === 'number')
                    ? (f.size / 1024 / 1024).toFixed(1)
                    : "0.0";

                // Escapa aspas simples para não quebrar o onclick
                const nomeSeguro = String(nomeArquivo).replace(/'/g, "\\'");

                return `
                    <li class="internal-file-item">
                        <div class="file-info">
                            <strong class="file-name-text">${nomeArquivo}</strong>
                            <small class="file-size-tag">${tamanhoMB} MB</small>
                        </div>
                        <button class="btn-print-internal"
                                onclick="imprimirArquivoInterno('${nomeSeguro}', event)">
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
// ✅ Substitua a função recuperarEstadoUploads
function recuperarEstadoUploads() {
    document.querySelectorAll('.card-pro').forEach(card => {
        const onclick = card.getAttribute('onclick') || '';
        const ipMatch = onclick.match(/'([^']+)'/);
        if (!ipMatch) return;

        const ip = ipMatch[1];
        const idLimpo = ip.split('.').join('-');

        fetch(`/progresso_transmissao/${ip}`)
            .then(r => r.json())
            .then(d => {
                // ✅ Mostra o loader se estiver enviando OU se deu erro (-1)
                if (d.msg !== "..." && (d.p < 100 || d.p === -1)) {
                    const loader = document.getElementById(`loader-${idLimpo}`);
                    if (loader) loader.style.display = 'flex';
                    iniciarMonitoramentoUpload(ip, idLimpo);
                }
            });
    });
}
function iniciarMonitoramentoUpload(ip, idLimpo) {
  if (!ip) return;

  // ✅ Evita dois loops simultâneos pro mesmo IP
  if (uploadsAtivos[ip]) {
    clearInterval(uploadsAtivos[ip]);
    delete uploadsAtivos[ip];
  }

  const loader = document.getElementById(`loader-${idLimpo}`);
  if (loader) loader.style.display = 'flex';

  // reset visual
  const fill = document.getElementById(`fill-${idLimpo}`);
  const pct  = document.getElementById(`pct-${idLimpo}`);
  if (fill) fill.style.width = '0%';
  if (pct)  pct.innerText = '0%';

  let ciclosIgnorados = 0;

  uploadsAtivos[ip] = setInterval(async () => {
    try {
      const resp = await fetch(`/progresso_transmissao/${ip}`, { cache: 'no-store' });

      // Se deu erro HTTP, loga e tenta novamente no próximo ciclo
      if (!resp.ok) {
        const txt = await resp.text().catch(() => '');
        console.warn(`progresso_transmissao HTTP ${resp.status} (${ip}):`, txt.slice(0, 300));
        return;
      }

      // Tenta ler JSON de forma segura
      const txt = await resp.text();
      let d = null;
      try {
        d = JSON.parse(txt);
      } catch (e) {
        console.warn(`progresso_transmissao retornou não-JSON (${ip}):`, txt.slice(0, 300));
        return;
      }

      // Se não tem status ainda, não mexe no UI
      if (!d || typeof d.p !== 'number' || d.msg === '...') return;

      const msgEl = document.querySelector(`#loader-${idLimpo} .status-msg`);

      if (fill) fill.style.width = `${d.p}%`;
      if (pct)  pct.innerText = `${d.p}%`;
      if (msgEl) msgEl.innerText = d.msg;

      // Ignora 100% fantasma (de estado anterior)
      if (d.p >= 100 && ciclosIgnorados < 2) {
        ciclosIgnorados++;
        return;
      }

      // Terminou ou deu erro
      if (d.p >= 100 || d.p === -1) {
        clearInterval(uploadsAtivos[ip]);
        delete uploadsAtivos[ip];
        setTimeout(() => finalizarVisualUpload(idLimpo), 2000);
      }
    } catch (err) {
      console.warn("Aguardando servidor de Betim...", ip, err);
    }
  }, 1000);
}

function finalizarVisualUpload(idLimpo) {
    const loader = document.getElementById(`loader-${idLimpo}`);
    const success = document.getElementById(`success-${idLimpo}`);

    // ✅ Apenas esconde o loader de transmissão, mantém o card visível
    if (loader) loader.style.opacity = '0.5'; 
    if (success) success.style.display = 'flex';

    setTimeout(() => {
        if (loader) loader.style.display = 'none';
        if (loader) loader.style.opacity = '1';
        if (success) success.style.display = 'none';
    }, 4000); // 4 segundos para você ter certeza do sucesso em Betim
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

    btn.onclick = async function() {
        const arquivo = this.dataset.arquivo;
        const ipAlvo = impressoraSelecionada; // 🛡️ Trava o IP aqui
        if (!arquivo || !ipAlvo) return alert("Selecione arquivo e impressora!");

        const idLimpo = ipAlvo.split('.').join('-');
        fecharModal(); 

        const loader = document.getElementById(`loader-${idLimpo}`);
        if (loader) loader.style.display = 'flex';

        fetch('/imprimir', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ ip: ipAlvo, arquivo: arquivo })
        }).then(r => {
            if (r.ok) iniciarMonitoramentoUpload(ipAlvo, idLimpo); // ✅ Agora o IP está garantido
        });
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

function setPasso(valor, btn) {
    passoAtual = valor;
    
    // Feedback visual: remove classe 'active' dos botões de passo e coloca no atual
    document.querySelectorAll('.jog-btn.step').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    console.log(`📏 Passo de movimento definido para: ${passoAtual}mm`);
}

/** * Movimentação relativa dinâmica (X, Y e Z)
 */
function moverImpressora(eixo, distancia) {
    const card = document.querySelector(`.card-pro[onclick*="${impressoraSelecionada}"]`);
    if (!card) {
        alert("⚠️ Selecione uma impressora ativa no painel primeiro.");
        return;
    }

    const statusAtivo = card.className;
    // Impede movimento acidental durante a produção em Betim
    if (statusAtivo.includes('printing')) {
        console.warn("🚫 Movimento bloqueado: Impressora está em produção.");
        return;
    }

    // G-Code: G91 (Relativo), G1 (Movimento), G90 (Absoluto)
    // F3000 (X/Y rápido) ou F600 (Z lento para segurança do motor)
    const feedrate = (eixo === 'Z') ? 600 : 3000;
    const gcode = `G91\nG1 ${eixo}${distancia} F${feedrate}\nG90`;
    
    console.log(`🕹️ Movendo ${eixo} em ${distancia}mm (Vel: ${feedrate})`);
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
let massaArquivoSelecionado = "";
let pastaAtualMassa = "";

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

function renderMassaParams() {
    const acao = document.getElementById('massaAcao')?.value;
    const containerArquivo = document.getElementById('containerNavegacaoMassa');
    const containerInputs = document.getElementById('containerInputsMassa');
    
    // Reset visual inicial
    containerArquivo.style.display = "none";
    containerInputs.style.display = "none";
    containerInputs.innerHTML = ""; // Limpa apenas os inputs extras

    if (acao === "PRINT_FILE") {
        // Mostra o navegador e esconde o resto
        containerArquivo.style.display = "block";
        if (!massaArquivoSelecionado) {
            carregarPastaMassa(""); // Só recarrega se não houver seleção
        }
    } 
    else if (acao === "SET_TEMP") {
        // Mostra os campos de temperatura e esconde o navegador
        containerInputs.style.display = "block";
        containerInputs.innerHTML = `
            <label style="display:block; margin:10px 0 6px;">Aquecer</label>
            <select id="massaTipoTemp" style="width:100%; padding:12px; border-radius:12px; background:#1a1a1a; color:white; border:1px solid rgba(255,255,255,0.1);">
                <option value="bico">Bico (M104)</option>
                <option value="mesa">Mesa (M140)</option>
            </select>
            <label style="display:block; margin:10px 0 6px;">Temperatura (°C)</label>
            <input id="massaTemp" type="number" placeholder="Ex: 200" style="width:100%; padding:12px; border-radius:12px; background:#1a1a1a; color:white; border:1px solid rgba(255,255,255,0.1);">
        `;
    } 
    else {
        // Outros comandos (Home, Pause, etc) não precisam de inputs extras
        containerInputs.style.display = "block";
        containerInputs.innerHTML = `<p style="opacity:.6; margin-top:10px; font-size:13px;">💡 Esta ação não requer parâmetros adicionais.</p>`;
    }
}

function carregarPastaMassa(caminho) {
    const ul = document.getElementById('listaArquivosMassa');
    if (!ul) return;

    ul.innerHTML = '<li class="loading-state" style="padding:15px; opacity:0.6; font-size:13px;">Buscando gcodes...</li>';

    fetch('/navegar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pasta: caminho })
    })
    .then(r => r.json())
    .then(data => {
        pastaAtualMassa = data.atual || "";
        document.getElementById('caminhoMassa').innerText = pastaAtualMassa || "Raiz";
        document.getElementById('btnVoltarMassa').disabled = (pastaAtualMassa === "");
        
        ul.innerHTML = "";

        // 1. Renderiza Pastas
        data.pastas.forEach(p => {
            const li = document.createElement('li');
            li.className = 'internal-file-item';
            li.innerHTML = `
                <div class="file-info">
                    <strong class="file-name-text">📁 ${p.nome}</strong>
                    <small class="file-size-tag">Diretório</small>
                </div>
                <button class="btn-massa-select" style="background:#444;">ABRIR</button>
            `;
            li.onclick = () => carregarPastaMassa(pastaAtualMassa ? `${pastaAtualMassa}/${p.nome}` : p.nome);
            ul.appendChild(li);
        });

        // 2. Renderiza Arquivos
        data.arquivos.forEach(f => {
            const rel = pastaAtualMassa ? `${pastaAtualMassa}/${f.nome}` : f.nome;
            const isGcode = f.nome.toLowerCase().endsWith('.gcode') || f.nome.toLowerCase().endsWith('.bgcode');
            
            const li = document.createElement('li');
            li.className = 'internal-file-item';
            // Mantém selecionado se você navegar e voltar
            if (massaArquivoSelecionado === rel) li.classList.add('selected-massa');

            li.innerHTML = `
                <div class="file-info">
                    <strong class="file-name-text">${f.nome}</strong>
                    <small class="file-size-tag">${f.tamanho}</small>
                </div>
                <button class="btn-massa-select" ${isGcode ? '' : 'disabled style="opacity:0.3;"'}>
                    ${isGcode ? 'SELECIONAR' : 'BLOQUEADO'}
                </button>
            `;

            li.onclick = (e) => {
                if (!isGcode) return;
                
                // Salva a seleção global
                massaArquivoSelecionado = rel;
                
                // Atualiza o painel de feedback
                document.getElementById('selecaoAtualMassa').style.display = "block";
                document.getElementById('nomeArquivoMassa').innerText = f.nome;
                
                // Feedback visual: remove de todos e coloca no clicado
                document.querySelectorAll('#listaArquivosMassa li').forEach(el => el.classList.remove('selected-massa'));
                li.classList.add('selected-massa');
            };
            ul.appendChild(li);
        });

        if (data.pastas.length === 0 && data.arquivos.length === 0) {
            ul.innerHTML = '<li class="empty-msg" style="padding:20px; text-align:center; opacity:0.5;">Pasta vazia</li>';
        }
    })
    .catch(err => {
        ul.innerHTML = '<li class="error-msg">Erro ao carregar biblioteca.</li>';
    });
}

function voltarPastaMassa() {
    let partes = pastaAtualMassa.split('/');
    partes.pop();
    carregarPastaMassa(partes.join('/'));
}

/* ===========================
   EXECUÇÃO EM MASSA
=========================== */
/* ===========================
   EXECUÇÃO EM MASSA (VERSÃO ATUALIZADA COM NAVEGADOR)
   =========================== */
function executarAcaoMassa() {
    // 1. Validação de Impressoras Selecionadas
    const ips = getSelectedIps();
    if (ips.length === 0) {
        alert("⚠️ Selecione pelo menos 1 impressora na grade antes de executar.");
        return;
    }

    const acao = document.getElementById('massaAcao')?.value;

    // --- CASO 1: IMPRESSÃO DE ARQUIVO ---
    if (acao === "PRINT_FILE") {
        // Valida se um arquivo foi clicado no navegador visual da modal
        if (!massaArquivoSelecionado) {
            alert("⚠️ Selecione um arquivo na lista da biblioteca antes de clicar em Executar.");
            return;
        }

        const confirmacao = confirm(`🚀 INICIAR PRODUÇÃO EM MASSA?\n\nArquivo: ${massaArquivoSelecionado}\nDestino: ${ips.length} impressoras selecionadas.`);
        if (!confirmacao) return;

        fetch('/imprimir_em_massa', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ips, arquivo: massaArquivoSelecionado })
        })
        .then(async r => {
            const res = await r.json();
            if (!r.ok) throw new Error(res.message || `Erro HTTP ${r.status}`);
            return res;
        })
        .then(res => {
            alert(`✅ Sucesso! Enfileirado para ${ips.length} impressoras.`);
            fecharModalMassa();
            
            // ✅ NOVO: Dispara o monitoramento para CADA impressora selecionada
            ips.forEach(ip => {
                const idLimpo = ip.split('.').join('-');
                iniciarMonitoramentoUpload(ip, idLimpo);
            });

            massaArquivoSelecionado = ""; 
        })
        .catch(err => alert("❌ Falha no envio em massa: " + err.message));

        return; // Encerra aqui para ações de impressão
    }

    // --- CASO 2: OUTROS COMANDOS (TEMPERATURA / MOVIMENTO / STATUS) ---
    let comando = "";

    if (acao === "SET_TEMP") {
        const tipo = document.getElementById('massaTipoTemp')?.value;
        const temp = document.getElementById('massaTemp')?.value;
        if (!temp || isNaN(temp)) {
            alert("⚠️ Informe uma temperatura válida.");
            return;
        }
        // Tradução para G-Code: M140 (Mesa) ou M104 (Bico)
        comando = (tipo === "mesa") ? `M140 S${temp}` : `M104 S${temp}`;
    } 
    else if (acao === "HOME_ALL") {
        comando = "G28";
    } 
    else if (acao === "PAUSE" || acao === "RESUME" || acao === "CANCEL") {
        comando = acao;
    } 
    else {
        alert("❌ Ação inválida ou não reconhecida.");
        return;
    }

    // Envio de comandos G-Code ou controle de estado em massa
    fetch('/api/comando_gcode_em_massa', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ips, comando })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            alert(`✅ Comando "${comando}" enviado com sucesso! (${res.ok || 0}/${res.total || ips.length})`);
            fecharModalMassa();
        } else {
            throw new Error(res.message || "Erro desconhecido");
        }
    })
    .catch(err => alert("🚨 Erro de rede ao enviar comando em massa: " + err.message));
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
