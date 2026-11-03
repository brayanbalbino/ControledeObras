"""
Controle orçamentário de pequenas obras - Planejado x Realizado

Rodar localmente:   python controle_obras.py   (ou dê dois cliques em iniciar.bat)
                    -> abre http://127.0.0.1:8050
Dados:              SQLite local (obras.db) por padrão; Firebase Firestore quando
                    FIREBASE_CREDENTIALS (ou serviceAccountKey.json) estiver presente.
Deploy:             Vercel (index.py na raiz) + Firebase. Veja LEIA-ME.md.

MAPA DO ARQUIVO (de cima para baixo)
  1. Ambiente ........ leitura do .env e instalação automática de bibliotecas
  2. Back-end ........ bancos de dados (SQLite / Firestore), cache e validação
  3. Helpers de front  formatação, indicadores e os 4 gráficos
  4. Front ........... CSS, modelo HTML e criação do app Dash
  5. Acesso .......... usuários, login/logout e permissões (admin x cliente)
  6. Telas ........... menu, dashboard, lançamentos e formulário
  7. Callbacks ....... ações da tela: navegar, inserir/excluir, atualizar, exportar
  8. Execução ........ bloco final que sobe o servidor no computador

Perfis: 'admin' vê e altera tudo; 'cliente' só lê as obras liberadas para ele.
Documentação detalhada de cada função: DOCUMENTACAO_DO_CODIGO.md
"""
import base64  # decodifica credenciais do Firebase guardadas em base64
import hashlib  # gera a chave secreta da sessão (SHA-256)
import hmac  # compara senhas em tempo constante
import importlib.util  # descobre se uma biblioteca está instalada
import json  # lê JSON (usuários e credenciais)
import math  # isnan/isinf: valida números digitados
import os  # variáveis de ambiente e arquivos
import sqlite3  # banco de dados local
import subprocess  # roda o pip para instalar dependências
import sys  # executável do Python e sys.exit
import threading  # abre o navegador sem travar o servidor
import time  # horários, cache e atrasos
import webbrowser  # abre o navegador automaticamente
from contextlib import contextmanager  # cria o gerenciador _con() do SQLite
from datetime import timedelta  # duração da sessão de login
from pathlib import Path  # caminhos que funcionam em Windows e Linux
from urllib.parse import quote  # deixa o ID do lançamento seguro como ID de documento

# Pasta onde este arquivo está. Usada para achar .env, usuarios.json e obras.db
# mesmo que o programa seja executado de outra pasta.
BASE = Path(__file__).resolve().parent
# True quando o código roda na Vercel (ela define a variável VERCEL). Muda o comportamento:
# não instala bibliotecas sozinho, grava dados em /tmp e exige usuários cadastrados.
NA_VERCEL = bool(os.environ.get("VERCEL"))


# ------------------------------------------------------- AMBIENTE / DEPENDÊNCIAS
# Preparação do ambiente: lê o .env e instala o que faltar antes de importar as bibliotecas.
def _carregar_env():
    """Lê o arquivo .env (se existir) sem precisar de python-dotenv."""
    arq = BASE / ".env"
    if not arq.exists():
        return
    for linha in arq.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            k, v = linha.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _garantir_dependencias():
    """Instala sozinho o que faltar (só no seu computador; na Vercel vem do requirements.txt)."""
    if NA_VERCEL:
        return
    pacotes = {"dash": "dash>=2.14", "plotly": "plotly", "openpyxl": "openpyxl"}
    faltam = [pip for mod, pip in pacotes.items() if importlib.util.find_spec(mod) is None]
    if not faltam:
        return
    print("Instalando dependências:", ", ".join(faltam), "...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *faltam])
    except subprocess.CalledProcessError:
        sys.exit(
            "\nNão consegui instalar automaticamente. Rode:\n"
            "  python -m pip install dash plotly openpyxl\n"
            "Se falhar, use o Python 3.12 ou 3.13 (o 3.15 ainda é pré-lançamento)."
        )


# Ordem importante: 1) lê o .env; 2) instala o que faltar; 3) só então importa dash/plotly/flask.
_carregar_env()
_garantir_dependencias()

# Bibliotecas de terceiros: plotly = gráficos; dash = páginas, tabela e callbacks;
# flask = servidor, rotas e sessão (o Dash roda em cima dele); werkzeug = verificação de hash de
# senha.
import plotly.graph_objects as go  # noqa: E402
from dash import Dash, Input, Output, State, ctx, dash_table, dcc, html, no_update  # noqa: E402
from dash import clientside_callback  # noqa: E402
from flask import Response, has_request_context, redirect, request, session  # noqa: E402
from werkzeug.security import check_password_hash  # noqa: E402

# Colunas esperadas da tabela SQLite. Se o arquivo existente tiver outras colunas (versão antiga),
# ele é guardado como backup e um banco novo é criado.
COLUNAS_DB = ["id", "id_lanc", "id_obra", "item", "qtd_p", "planejado", "qtd_r", "realizado"]


# ------------------------------------------------------------------ BACK-END
# Camada de dados. Existem dois 'bancos' (SQLiteStore e FirestoreStore) com a MESMA interface:
# listar(), existe(), inserir() e excluir(). Assim o resto do código não sabe qual está em uso.
# Erro próprio, levantado pelos dois bancos quando o ID_Lancamento já existe.
class Duplicado(Exception):
    """ID_Lancamento já existe."""


# Banco LOCAL em arquivo (obras.db). Usado no computador e, na Vercel, só como modo demonstração.
class SQLiteStore:
    nome = "SQLite local"  # texto mostrado na barra lateral do administrador
    temporario = False
    ttl = 0  # sem cache: leitura local é instantânea

    def __init__(self, caminho: Path):
        """Guarda o caminho do arquivo e prepara (cria ou valida) a tabela."""
        self.caminho = caminho
        self._preparar()

    @contextmanager
    def _con(self):
        """Abre uma conexão, confirma (commit) ao final e SEMPRE fecha. Fechar é importante
        no Windows, para liberar o arquivo."""
        con = sqlite3.connect(self.caminho)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()  # importante no Windows: libera o arquivo

    def _preparar(self):
        """Cria a tabela se não existir, confere as colunas (arquiva o banco se for de versão
        antiga) e cria o índice único."""
        with self._con() as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS lancamentos (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    id_lanc   TEXT NOT NULL,
                    id_obra   TEXT NOT NULL,
                    item      TEXT NOT NULL,
                    qtd_p     REAL NOT NULL CHECK (qtd_p >= 0),
                    planejado REAL NOT NULL CHECK (planejado >= 0),
                    qtd_r     REAL NOT NULL CHECK (qtd_r >= 0),
                    realizado REAL NOT NULL CHECK (realizado >= 0)
                )"""
            )
            # PRAGMA table_info lista as colunas atuais da tabela, para comparar com COLUNAS_DB.
            cols = [r[1] for r in con.execute("PRAGMA table_info(lancamentos)")]
        if cols != COLUNAS_DB:  # banco de uma versão antiga: guarda backup e recomeça
            bak = self.caminho.with_name(f"obras_backup_{time.strftime('%Y%m%d_%H%M%S')}.db")
            self.caminho.replace(bak)
            print(f"Aviso: esquema antigo. Backup salvo em {bak.name}; criando banco novo.")
            # Recomeça: agora a tabela nova é criada do zero.
            return self._preparar()
        with self._con() as con:
            try:
                con.execute(
                    # índice único sem diferenciar maiúsculas de minúsculas: o próprio banco
                    # impede ID repetido
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_id_lanc ON lancamentos (id_lanc COLLATE NOCASE)"
                )
            except sqlite3.IntegrityError:  # já havia IDs repetidos no arquivo: não dá para criar o índice único
                print("Aviso: há ID_Lancamento duplicados; índice único não criado.")

    def listar(self):
        """Devolve todos os lançamentos como lista de dicionários, na ordem de cadastro."""
        with self._con() as con:
            return [dict(r) for r in con.execute("SELECT * FROM lancamentos ORDER BY id")]

    def existe(self, id_lanc):
        """True se já existe lançamento com esse ID (sem diferenciar maiúsculas)."""
        with self._con() as con:
            return con.execute(
                "SELECT 1 FROM lancamentos WHERE id_lanc = ? COLLATE NOCASE LIMIT 1", (id_lanc,)
            ).fetchone() is not None

    def inserir(self, id_lanc, id_obra, item, qtd_p, planejado, qtd_r, realizado):
        """Grava um lançamento. Levanta Duplicado se o ID já existir."""
        try:
            with self._con() as con:
                con.execute(
                    "INSERT INTO lancamentos (id_lanc,id_obra,item,qtd_p,planejado,qtd_r,realizado) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (id_lanc, id_obra, item, qtd_p, planejado, qtd_r, realizado),
                )
        except sqlite3.IntegrityError as e:  # violou o índice único: ID repetido
            raise Duplicado from e

    def excluir(self, ids):
        """Apaga os lançamentos cujos 'id' foram informados."""
        with self._con() as con:
            con.executemany("DELETE FROM lancamentos WHERE id = ?", [(i,) for i in ids])


# Banco NA NUVEM (Firebase Firestore). Cada lançamento é um 'documento' dentro de uma coleção.
class FirestoreStore:
    nome = "Firebase Firestore"  # texto mostrado na barra lateral do administrador
    temporario = False
    ttl = 15  # segundos de cache em memória: economiza leituras (cobradas pelo Firebase)

    def __init__(self, credenciais: dict):
        """Conecta ao Firebase com a chave de serviço e guarda a coleção onde os lançamentos
        ficam."""
        # importado aqui dentro: só quem usa o Firebase precisa ter a biblioteca instalada.
        import firebase_admin
        from firebase_admin import credentials, firestore

        # evita inicializar o SDK duas vezes (daria erro).
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(credenciais))
        nome_colecao = os.environ.get("FIREBASE_COLLECTION", "lancamentos")
        # self.col = a 'pasta' (coleção) do Firestore onde ficam os lançamentos.
        self.col = firestore.client().collection(nome_colecao)

    @staticmethod
    def _doc_id(id_lanc):
        """Converte o ID_Lancamento em ID de documento: minúsculo, com caracteres especiais
        codificados e prefixo 'l_'. É o que garante o ID único."""
        # o ID do documento é o próprio ID_Lancamento (minúsculo): garante unicidade
        return "l_" + quote(id_lanc.strip().lower(), safe="")

    def listar(self):
        """Lê todos os documentos da coleção (stream), guarda o ID de cada um e ordena por
        'criado_em'."""
        linhas = []
        for d in self.col.stream():
            x = d.to_dict()
            # guarda o ID do documento para poder excluir depois
            x["id"] = d.id
            linhas.append(x)
        # mantém a ordem de cadastro (o Firestore não garante ordem).
        linhas.sort(key=lambda x: x.get("criado_em", 0))
        return linhas

    def existe(self, id_lanc):
        """True se o documento com esse ID já existe (uma leitura no Firestore)."""
        return self.col.document(self._doc_id(id_lanc)).get().exists

    def inserir(self, id_lanc, id_obra, item, qtd_p, planejado, qtd_r, realizado):
        """Grava o lançamento com create(), que falha se o ID já existir (vira Duplicado)."""
        # erro que o Firestore devolve quando .create() encontra o documento já existente.
        from google.api_core.exceptions import AlreadyExists

        try:
            # create() falha se o documento já existe: isso garante o ID único mesmo com dois
            # acessos ao mesmo tempo.
            self.col.document(self._doc_id(id_lanc)).create(
                {
                    "id_lanc": id_lanc, "id_obra": id_obra, "item": item,
                    "qtd_p": qtd_p, "planejado": planejado,
                    "qtd_r": qtd_r, "realizado": realizado,
                    "criado_em": time.time(),  # momento do cadastro, usado para ordenar
                }
            )
        except AlreadyExists as e:
            raise Duplicado from e

    def excluir(self, ids):
        """Apaga os documentos informados, em lotes de até 400 (o limite do Firestore é 500
        por lote)."""
        from firebase_admin import firestore

        ids = list(ids)
        for i in range(0, len(ids), 400):  # limite de 500 operações por lote
            # lote (batch): apaga vários documentos de uma só vez.
            lote = firestore.client().batch()
            for doc_id in ids[i : i + 400]:
                lote.delete(self.col.document(doc_id))
            lote.commit()


def _credenciais_firebase():
    """JSON da conta de serviço: variável FIREBASE_CREDENTIALS (JSON ou base64) ou arquivo."""
    bruto = os.environ.get("FIREBASE_CREDENTIALS", "").strip()
    if bruto:
        # se não começa com '{', o conteúdo está codificado em base64.
        if not bruto.startswith("{"):
            bruto = base64.b64decode(bruto).decode("utf-8")
        return json.loads(bruto)
    # alternativa para uso local: arquivo da chave (serviceAccountKey.json).
    for caminho in (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"), BASE / "serviceAccountKey.json"):
        if caminho and Path(caminho).exists():
            return json.loads(Path(caminho).read_text(encoding="utf-8"))
    return None


def criar_store():
    """Decide qual banco usar: Firestore se houver credenciais; senão SQLite (em /tmp na
    Vercel, na pasta do projeto no computador)."""
    try:
        cred = _credenciais_firebase()
        if cred:
            return FirestoreStore(cred)
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: Firebase indisponível ({e}). Usando SQLite.")
    if NA_VERCEL:  # na Vercel só /tmp é gravável (e some a qualquer momento)
        s = SQLiteStore(Path("/tmp/obras.db"))
        s.nome, s.temporario = "Modo demonstração (dados temporários)", True
        return s
    return SQLiteStore(BASE / "obras.db")


# STORE = o banco em uso. O resto do código só chama STORE.listar/existe/inserir/excluir.
STORE = criar_store()
# Cache em memória da tabela já calculada: t = quando foi lida; dados = a lista.
_cache = {"t": 0.0, "dados": None}


def erro_curto(e, limite=280):
    """Resume mensagens de erro longas (ex.: do Firestore) para caberem no aviso da tela."""
    texto = " ".join(str(e).split())
    return (texto[:limite] + "…") if len(texto) > limite else texto


def invalidar_cache():
    """Descarta o cache. Chamada depois de inserir/excluir, para a próxima leitura trazer
    dados novos."""
    _cache["dados"] = None


def carregar_tabela():
    """Lê os lançamentos e devolve dicionários já com os cálculos."""
    # só vai ao banco se não há cache ou se ele expirou (ttl = 0 no SQLite: sempre lê; 15 s no
    # Firestore).
    if _cache["dados"] is None or time.time() - _cache["t"] > STORE.ttl:
        dados = []
        for r in STORE.listar():
            # totais = quantidade x valor unitário (planejado e realizado).
            tp, tr = r["qtd_p"] * r["planejado"], r["qtd_r"] * r["realizado"]
            dados.append(
                {
                    "id": r["id"],
                    "ID_Lancamento": r["id_lanc"],
                    "ID_Obra": r["id_obra"],
                    "ITENS": r["item"],
                    "QTD P": r["qtd_p"],
                    "PLANEJADO": r["planejado"],
                    "QTD R": r["qtd_r"],
                    "REALIZADO": r["realizado"],
                    "DIFERENÇA": r["realizado"] - r["planejado"],  # diferença unitária
                    "TOTAL PLANEJADO": tp,
                    "TOTAL REALIZADO": tr,
                    "DIF. TOTAL": tr - tp,  # positivo = estouro; negativo = economia
                }
            )
        _cache.update(t=time.time(), dados=dados)
    return _cache["dados"]


def validar_lancamento(id_lanc, id_obra, item, qtd_p, plan, qtd_r, real):
    """Devolve a mensagem de erro, ou None se estiver tudo certo."""
    # Ordem das regras: 1) campos preenchidos; 2) numéricos; 3) válidos; 4) sem negativos; 5) ID
    # único.
    textos, numeros = [id_lanc, id_obra, item], [qtd_p, plan, qtd_r, real]
    if any(t is None or not str(t).strip() for t in textos) or any(n is None for n in numeros):
        return "Preencha todos os campos."
    try:
        numeros = [float(n) for n in numeros]
    except (TypeError, ValueError):
        return "Quantidades e valores devem ser numéricos."
    if any(math.isnan(n) or math.isinf(n) for n in numeros):
        return "Quantidades e valores devem ser numéricos válidos."
    if any(n < 0 for n in numeros):
        return "Quantidades e valores não podem ser negativos."
    # única regra que consulta o banco.
    if STORE.existe(str(id_lanc).strip()):
        return f"O ID_Lancamento '{str(id_lanc).strip()}' já existe. Use um ID diferente."
    return None


# ------------------------------------------------------------ HELPERS DE FRONT
# Funções que montam o que aparece na tela (textos, números e gráficos). Não acessam o banco.
# Paleta usada nos gráficos (espelha as variáveis do CSS): INK = verde-petróleo escuro; PLAN =
# azul (planejado);
# REAL = âmbar (realizado); GOOD = verde (economia); BAD = vermelho (estouro); GRID = linhas de
# grade; STEM = hastes.
INK, PLAN, REAL = "#0F2326", "#3F6E8C", "#F2A33A"
GOOD, BAD, GRID, STEM = "#25946A", "#D9483B", "#E3E9E7", "#C5D0CD"
MUTED = "#5E7174"
PALETA = ["#F2A33A", "#3F6E8C", "#2A9D8F", "#8C5E8A", "#7A8F3D", "#C98B5E", "#5B6B6E", "#B0C4C0"]
# Fontes dos gráficos (as mesmas carregadas pelo CSS).
FONTE = "Figtree, Segoe UI, sans-serif"
FONTE_TITULO = "Bricolage Grotesque, Segoe UI, sans-serif"


def brl(v) -> str:
    """Formata número como moeda brasileira: 1234.5 vira 'R$ 1.234,50' (negativos com '-')."""
    v = round(float(v or 0), 2)
    if v == 0:
        # troca -0.0 por 0.0, para não aparecer '-R$ 0,00'
        v = 0.0
    # formata no padrão americano (1,234.50) e troca os separadores para o brasileiro (1.234,50).
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if v < 0 else ''}R$ {s}"


def curto(texto, n=28):
    """Encurta textos longos com '…' (nomes dentro dos gráficos)."""
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def filtrar(dados, obra, busca):
    """Aplica os filtros da barra superior: obra escolhida e texto de busca (item, lançamento
    ou obra)."""
    if obra:
        dados = [d for d in dados if d["ID_Obra"] == obra]
    if busca and busca.strip():
        b = busca.strip().lower()
        dados = [d for d in dados if b in d["ITENS"].lower() or b in d["ID_Lancamento"].lower() or b in d["ID_Obra"].lower()]
    return dados


def totais(dados):
    """Soma e devolve (total planejado, total realizado) da lista."""
    return sum(d["TOTAL PLANEJADO"] for d in dados), sum(d["TOTAL REALIZADO"] for d in dados)


def kpi(titulo, valor, sub="", tom=""):
    """Monta um cartão de indicador: título, valor grande e subtítulo. 'tom' define a cor
    (kpi--plan, kpi--real, kpi--good, kpi--bad)."""
    return html.Div(
        className=f"kpi {tom}".strip(),
        children=[
            html.Div(titulo, className="kpi__label"),
            html.Div(valor, className="kpi__value"),
            html.Div(sub, className="kpi__sub"),
        ],
    )


def montar_kpis(dados):
    """Cria os 4 cartões do topo: Planejado, Realizado, Desvio (em R$ e %) e Lançamentos."""
    plan, real = totais(dados)
    desvio = real - plan
    pct = (desvio / plan * 100) if plan else 0.0  # evita divisão por zero
    tom = "kpi--bad" if desvio > 0 else "kpi--good" if desvio < 0 else ""  # vermelho se estourou, verde se economizou
    estouros = sum(1 for d in dados if d["DIF. TOTAL"] > 0)
    pct_txt = f"{pct:+.1f}%".replace(".", ",")
    return [
        kpi("Planejado", brl(plan), "Orçamento previsto", "kpi--plan"),
        kpi("Realizado", brl(real), "Gasto efetivo", "kpi--real"),
        kpi("Desvio", brl(desvio), f"{pct_txt} sobre o planejado", tom),
        kpi("Lançamentos", str(len(dados)), f"{estouros} item(ns) estourado(s)", "kpi--bad" if estouros else ""),
    ]


def montar_resumo(dados):
    """Frase de destaque ao lado do velocímetro: quanto está acima ou abaixo do orçamento."""
    if not dados:
        return html.Div("Cadastre o primeiro lançamento para acompanhar a obra.", className="hero__sub")
    plan, real = totais(dados)
    desvio = real - plan
    if desvio > 0:
        titulo, tom = f"{brl(desvio)} acima do orçamento", "bad"
    elif desvio < 0:
        titulo, tom = f"{brl(-desvio)} abaixo do orçamento", "good"
    else:
        titulo, tom = "Dentro do orçamento", ""
    return html.Div(
        [
            html.Div(titulo, className=f"hero__headline {tom}"),
            html.Div(f"{brl(real)} gastos de {brl(plan)} previstos", className="hero__sub"),
        ]
    )


def _base(fig: go.Figure) -> go.Figure:
    """Aplica o visual comum a todos os gráficos: fundo transparente, fonte, margens,
    legenda, hover e separadores brasileiros (separators=',.' = decimal vírgula, milhar
    ponto)."""
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": FONTE, "size": 12, "color": "#2B3F42"},
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        legend={"orientation": "h", "y": 1.14, "x": 0, "font": {"size": 12}},
        hoverlabel={"bgcolor": INK, "font": {"color": "white", "family": FONTE}, "bordercolor": INK},
        separators=",.",
    )
    return fig


def fig_vazia(texto="Sem lançamentos para exibir"):
    """Gráfico 'vazio' com uma mensagem, para quando não há dados."""
    fig = go.Figure()
    fig.add_annotation(text=texto, showarrow=False, font={"size": 14, "color": "#8FA0A3"})
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _base(fig)


def fig_gauge(dados):
    """Velocímetro: % executado = realizado / planejado x 100. A barra fica vermelha acima de
    100%, e há uma marca na linha dos 100%."""
    plan, real = totais(dados)
    pct = (real / plan * 100) if plan else 0.0
    # fim da escala: no mínimo 120%, crescendo de 20 em 20 se o gasto passar disso.
    teto = max(120, (int(pct // 20) + 1) * 20)
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=pct,
            number={"suffix": "%", "valueformat": ".0f", "font": {"size": 46, "family": FONTE_TITULO, "color": INK}},
            gauge={
                "axis": {"range": [0, teto], "ticksuffix": "%", "tickfont": {"size": 11, "color": MUTED}, "tickvals": [0, 50, 100, teto]},
                "bar": {"color": BAD if pct > 100 else REAL, "thickness": 0.34},
                "bgcolor": "rgba(0,0,0,0)",
                "borderwidth": 0,
                "steps": [{"range": [0, 100], "color": "#E4EEEA"}, {"range": [100, teto], "color": "#F7DEDA"}],
                "threshold": {"line": {"color": INK, "width": 3}, "thickness": 0.9, "value": 100},
            },
        )
    )
    fig.update_layout(margin={"l": 30, "r": 30, "t": 24, "b": 0})
    return _base(fig)


def fig_obras(dados):
    """Planejado x Realizado por obra: gráfico de 'halteres' (dois pontos ligados por uma haste)."""
    if not dados:
        return fig_vazia()
    agg = {}
    for d in dados:
        # agg[obra] = [total planejado, total realizado], somando todos os itens da obra.
        t = agg.setdefault(d["ID_Obra"], [0.0, 0.0])
        t[0] += d["TOTAL PLANEJADO"]
        t[1] += d["TOTAL REALIZADO"]
    nomes = sorted(agg, key=lambda n: agg[n][1])[-8:]  # maior gasto no topo
    # haste = linha que liga o ponto planejado ao realizado; o None quebra a linha entre uma obra
    # e outra.
    haste_x, haste_y = [], []
    for n in nomes:
        haste_x += [agg[n][0], agg[n][1], None]
        haste_y += [n, n, None]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=haste_x, y=haste_y, mode="lines", line={"color": STEM, "width": 5}, hoverinfo="skip", showlegend=False))
    # desenha os dois pontos de cada obra (azul = planejado, âmbar = realizado).
    for rotulo, idx, cor in (("Planejado", 0, PLAN), ("Realizado", 1, REAL)):
        vals = [agg[n][idx] for n in nomes]
        fig.add_trace(
            go.Scatter(
                x=vals, y=nomes, mode="markers", name=rotulo,
                marker={"size": 18, "color": cor, "line": {"color": "#fff", "width": 3}},
                customdata=[brl(v) for v in vals],
                hovertemplate="%{y}<br>" + rotulo + ": %{customdata}<extra></extra>",
            )
        )
    fig.update_xaxes(tickprefix="R$ ", tickformat=",.0f", gridcolor=GRID, zeroline=False)
    fig.update_yaxes(tickvals=nomes, ticktext=[curto(n) for n in nomes], automargin=True, showgrid=False)
    return _base(fig)


def fig_donut(dados):
    """Rosca: como o gasto REALIZADO se divide entre as obras (só entram obras com gasto >
    0). O total fica no centro."""
    agg = {}
    for d in dados:
        agg[d["ID_Obra"]] = agg.get(d["ID_Obra"], 0.0) + d["TOTAL REALIZADO"]
    agg = {k: v for k, v in agg.items() if v > 0}
    if not agg:
        return fig_vazia("Sem valores realizados ainda")
    nomes = sorted(agg, key=agg.get, reverse=True)
    fig = go.Figure(
        go.Pie(
            labels=[curto(n, 26) for n in nomes],
            values=[agg[n] for n in nomes],
            hole=0.68, sort=False, direction="clockwise",
            marker={"colors": [PALETA[i % len(PALETA)] for i in range(len(nomes))], "line": {"color": "#fff", "width": 3}},
            textinfo="percent", textfont={"color": "white", "size": 12}, insidetextorientation="horizontal",
            customdata=[[brl(agg[n]), n] for n in nomes],
            hovertemplate="%{customdata[1]}<br>%{customdata[0]} (%{percent})<extra></extra>",
        )
    )
    fig.add_annotation(text=f"<b>{brl(sum(agg.values()))}</b><br>realizado", showarrow=False, font={"size": 16, "family": FONTE_TITULO, "color": INK})
    fig.update_layout(legend={"orientation": "h", "y": -0.05, "x": 0.5, "xanchor": "center"})
    return _base(fig)


def fig_itens(dados):
    """Maiores desvios por item: 'pirulitos' (haste + ponto), vermelho = estouro, verde = economia."""
    itens = {}
    for d in dados:
        # soma o desvio por NOME de item (o mesmo item em várias obras é somado).
        itens[d["ITENS"]] = itens.get(d["ITENS"], 0.0) + d["DIF. TOTAL"]
    # escolhe os 8 itens de maior desvio (em valor absoluto), ignorando os de desvio zero.
    top = [kv for kv in sorted(itens.items(), key=lambda kv: abs(kv[1]), reverse=True)[:8] if kv[1] != 0]
    if not top:
        return fig_vazia("Nenhum desvio nos itens")
    top.sort(key=lambda kv: kv[1])  # maior estouro no topo
    nomes = [k for k, _ in top]
    # haste de cada 'pirulito': vai de zero até o desvio do item.
    haste_x, haste_y = [], []
    for n, v in top:
        haste_x += [0, v, None]
        haste_y += [n, n, None]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=haste_x, y=haste_y, mode="lines", line={"color": STEM, "width": 3}, hoverinfo="skip", showlegend=False))
    # dois grupos de pontos: estouro (vermelho, valor à direita) e economia (verde, valor à
    # esquerda).
    for rotulo, cond, cor, pos in (
        ("Estouro", lambda v: v > 0, BAD, "middle right"),
        ("Economia", lambda v: v < 0, GOOD, "middle left"),
    ):
        pts = [(n, v) for n, v in top if cond(v)]
        if pts:
            fig.add_trace(
                go.Scatter(
                    x=[v for _, v in pts], y=[n for n, _ in pts], mode="markers+text", name=rotulo,
                    text=[brl(v) for _, v in pts], textposition=pos, textfont={"size": 11, "color": cor}, cliponaxis=False,
                    marker={"size": 14, "color": cor, "line": {"color": "#fff", "width": 2}},
                    hovertemplate="%{y}<br>%{text}<extra></extra>",
                )
            )
    # folga nas laterais, para os rótulos de valor não serem cortados.
    lim = max(abs(v) for _, v in top) * 1.45
    fig.update_xaxes(range=[-lim if min(v for _, v in top) < 0 else -lim * 0.1, lim if max(v for _, v in top) > 0 else lim * 0.1],
                     tickprefix="R$ ", tickformat=",.0f", gridcolor=GRID, zeroline=True, zerolinecolor="#8FA0A3")
    fig.update_yaxes(tickvals=nomes, ticktext=[curto(n, 30) for n in nomes], automargin=True, showgrid=False)
    return _base(fig)


# ---------------------------------------------------------------------- FRONT
# Interface: CSS (aparência), modelo HTML e criação do app Dash. O Dash transforma componentes
# Python em página web.
# CSS de todo o sistema, guardado como texto e injetado na página (index_string). Começa com as
# variáveis de cor e fonte (:root),
# depois menu lateral, barra superior, cartões, formulário, tabela, login e ajustes para celular
# (@media).
CSS_ESTILO = r"""
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=Figtree:wght@400;500;600;700&display=swap');
:root {
  --ink: #0f2326; --ink-2: #18383c; --bg: #eef1f0; --card: #fff; --line: #dde4e2;
  --text: #17282b; --muted: #5e7174; --plan: #3f6e8c; --real: #f2a33a;
  --good: #25946a; --good-bg: #e7f5ee; --bad: #d9483b; --bad-bg: #fcebe8;
  --radius: 16px; --shadow: 0 1px 2px rgba(15, 35, 38, .05), 0 6px 18px rgba(15, 35, 38, .05);
  --display: "Bricolage Grotesque", "Segoe UI", sans-serif;
  --body: "Figtree", "Segoe UI", system-ui, sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--body); -webkit-font-smoothing: antialiased; }
:focus-visible { outline: 2px solid var(--real); outline-offset: 2px; }
.app { display: flex; min-height: 100vh; }

/* Sidebar */
.sidebar { width: 252px; flex-shrink: 0; background: var(--ink); color: #fff; display: flex; flex-direction: column; padding: 24px 14px; position: sticky; top: 0; height: 100vh; }
.brand { display: flex; align-items: center; gap: 12px; padding: 2px 10px 30px; }
.brand__logo { width: 42px; height: 42px; border-radius: 12px; background: var(--real); color: var(--ink); display: grid; place-items: center; font: 700 17px var(--display); }
.brand__name { font: 700 16px var(--display); }
.brand__sub { font-size: 12px; color: #8fb0b3; margin-top: 2px; }
.nav { display: flex; flex-direction: column; gap: 4px; }
.nav__link { display: flex; align-items: center; gap: 12px; padding: 11px 12px; border-radius: 10px; color: #b4cacc; text-decoration: none; font-size: 14.5px; font-weight: 500; border-left: 3px solid transparent; transition: background .15s, color .15s; }
.nav__link:hover { background: rgba(255, 255, 255, .06); color: #fff; }
.nav__link--active, .nav__link--active:hover { background: rgba(255, 255, 255, .09); color: #fff; font-weight: 600; border-left-color: var(--real); }
.nav__icon { width: 20px; text-align: center; font-size: 17px; }
.sidebar__bottom { margin-top: auto; }
.sidebar__foot { padding: 12px 10px 0; font-size: 12px; color: #8fb0b3; display: flex; align-items: center; gap: 8px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--good); flex-shrink: 0; }
.dot--warn { background: var(--real); }

/* Principal */
.main { flex: 1; min-width: 0; padding: 24px 32px 48px; }
.page-title { font: 700 30px var(--display); letter-spacing: -.3px; margin: 22px 0 18px; color: var(--ink); }
.topbar { display: flex; align-items: center; justify-content: space-between; gap: 14px; background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 16px; box-shadow: var(--shadow); flex-wrap: wrap; }
.topbar__filters { display: flex; gap: 10px; flex: 1; min-width: 260px; flex-wrap: wrap; }
.filter--obra { width: 320px; max-width: 100%; }
.filter--busca { flex: 1; min-width: 220px; }

/* Campos e botões */
.input { width: 100%; height: 42px; padding: 0 14px; border: 1px solid #cfd9d6; border-radius: 12px; background: #fff; font: 500 14px var(--body); color: var(--text); outline: none; }
.input:focus { border-color: var(--ink-2); box-shadow: 0 0 0 3px rgba(242, 163, 58, .35); }
.filter--obra .Select-control, .filter--obra .dash-dropdown { border-radius: 12px; min-height: 42px; }
.btn { height: 42px; padding: 0 20px; border: 0; border-radius: 12px; font: 600 14px var(--body); cursor: pointer; transition: filter .15s, transform .05s; }
.btn:hover { filter: brightness(1.06); }
.btn:active { transform: translateY(1px); }
.btn--primary { background: var(--ink); color: #fff; }
.btn--accent { background: var(--real); color: var(--ink); }
.btn--ghost { background: #fff; color: var(--ink); border: 1px solid #cfd9d6; }
.btn--danger { background: var(--bad-bg); color: var(--bad); border: 1px solid #f3c4be; }

/* Aviso (toast) */
.toast-wrap { position: fixed; right: 24px; bottom: 24px; z-index: 50; pointer-events: none; }
.msg { display: block; padding: 13px 18px; border-radius: 12px; font-size: 14px; font-weight: 600; box-shadow: 0 10px 30px rgba(15, 35, 38, .2); animation: toast 6s ease forwards; }
.msg--ok { background: var(--ink); color: #fff; border-left: 5px solid var(--good); }
.msg--err { background: #fff; color: var(--bad); border-left: 5px solid var(--bad); }
@keyframes toast { 0% { opacity: 0; } 6% { opacity: 1; } 88% { opacity: 1; visibility: visible; } 100% { opacity: 0; visibility: hidden; } }

/* Dashboard */
.hero { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.35fr); gap: 16px; margin-bottom: 16px; }
.hero__headline { font: 700 22px var(--display); color: var(--ink); text-align: center; }
.hero__headline.bad { color: var(--bad); }
.hero__headline.good { color: var(--good); }
.hero__sub { font-size: 13.5px; color: var(--muted); margin-top: 4px; text-align: center; }
.kpis { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.kpi { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px 20px; box-shadow: var(--shadow); display: flex; flex-direction: column; justify-content: center; }
.kpi__label { font-size: 13.5px; color: var(--muted); font-weight: 600; display: flex; align-items: center; gap: 8px; }
.kpi__label::before { content: ""; width: 9px; height: 9px; border-radius: 50%; background: #b9c6c4; }
.kpi__value { font: 700 28px var(--display); margin: 8px 0 2px; color: var(--ink); white-space: nowrap; }
.kpi__sub { font-size: 12.5px; color: var(--muted); }
.kpi--plan .kpi__label::before { background: var(--plan); }
.kpi--real .kpi__label::before { background: var(--real); }
.kpi--good .kpi__label::before { background: var(--good); }
.kpi--bad .kpi__label::before { background: var(--bad); }
.kpi--good .kpi__value { color: var(--good); }
.kpi--bad .kpi__value { color: var(--bad); }

.charts { display: grid; grid-template-columns: 1.5fr 1fr; gap: 16px; margin-bottom: 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); padding: 20px 22px; box-shadow: var(--shadow); min-width: 0; }
.card__head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; gap: 10px; flex-wrap: wrap; }
.card__title { margin: 0 0 4px; font: 700 17px var(--display); color: var(--ink); }
.card__head .card__title { margin: 0; }
.card__hint { font-size: 12.5px; color: var(--muted); margin-top: 8px; }

/* Formulário */
.form { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-top: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field label { font-size: 13px; font-weight: 600; color: var(--muted); }
.field--wide { grid-column: span 2; }
.form__actions { margin-top: 20px; }
.previa { display: flex; gap: 28px; flex-wrap: wrap; margin-top: 20px; padding: 14px 18px; border-radius: 12px; background: var(--bg); }
.previa__label { font-size: 12.5px; color: var(--muted); }
.previa__value { font: 700 18px var(--display); color: var(--ink); }
.previa__value.bad { color: var(--bad); }
.previa__value.good { color: var(--good); }

/* Tabela */
.dash-spreadsheet-container .dash-spreadsheet-inner th { white-space: nowrap; }

/* Usuário logado e página de login */
.page-sub { margin: -10px 0 18px; color: var(--muted); font-size: 14.5px; }
.userbox { padding: 14px 12px 4px; border-top: 1px solid rgba(255, 255, 255, .1); }
.userbox__nome { font-weight: 600; font-size: 14px; }
.userbox__papel { font-size: 12px; color: #8fb0b3; margin: 2px 0 8px; }
.userbox__sair { font-size: 13px; color: #fff; text-decoration: underline; text-underline-offset: 3px; }
.login-body { display: grid; place-items: center; min-height: 100vh; margin: 0; padding: 20px; }
.login { width: min(400px, 100%); background: #fff; border: 1px solid var(--line); border-radius: var(--radius); padding: 32px 30px; box-shadow: var(--shadow); }
.login .brand { padding: 0 0 22px; }
.login .brand__name { color: var(--ink); }
.login .brand__sub { color: var(--muted); }
.login h1 { font: 700 24px var(--display); margin: 0 0 16px; color: var(--ink); }
.login .field { margin-bottom: 14px; }
.login .btn { width: 100%; margin-top: 6px; }
.login__erro { background: var(--bad-bg); color: var(--bad); border-radius: 10px; padding: 10px 12px; font-size: 13.5px; font-weight: 600; margin: 0 0 14px; }

/* Responsivo */
@media (max-width: 1200px) {
  .hero, .charts { grid-template-columns: 1fr; }
  .form { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 860px) {
  .app { flex-direction: column; }
  .sidebar { width: 100%; height: auto; position: static; flex-direction: row; align-items: center; padding: 12px; gap: 12px; flex-wrap: wrap; }
  .brand { padding: 0; }
  .nav { flex-direction: row; flex-wrap: wrap; }
  .sidebar__foot { display: none; }
  .sidebar__bottom { margin-top: 0; }
  .userbox { border: 0; padding: 0; }
  .main { padding: 16px; }
  .kpis { grid-template-columns: 1fr 1fr; }
  .form { grid-template-columns: 1fr; }
  .field--wide { grid-column: span 1; }
}
"""

# Modelo da página HTML do Dash. Os marcadores {%...%} são do Dash (título, CSS, scripts); _CSS_ é
# trocado por CSS_ESTILO.
INDEX_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <style>_CSS_</style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>"""

# Cria o aplicativo Dash. suppress_callback_exceptions=True permite callbacks para componentes que
# só existem depois do layout montado.
app = Dash(__name__, suppress_callback_exceptions=True)
app.title = "Controle de Obras"
app.index_string = INDEX_HTML.replace("_CSS_", CSS_ESTILO)
server = app.server  # a Vercel usa este objeto (WSGI)

# ------------------------------------------------------------ USUÁRIOS E ACESSO
# Login e permissões. Tudo é verificado no SERVIDOR, não só escondido na tela.
# Perfis: "admin" (vê e altera tudo) e "cliente" (somente leitura, só das obras dele).
# Cadastro: variável USUARIOS (JSON) ou arquivo usuarios.json. Veja usuarios.example.json.
def _carregar_usuarios():
    """Lê USUARIOS (variável de ambiente) ou usuarios.json e devolve {login em minúsculo:
    dados}. O papel padrão é 'cliente' (menor privilégio)."""
    bruto = os.environ.get("USUARIOS", "").strip()
    arq = BASE / "usuarios.json"
    if not bruto and arq.exists():
        bruto = arq.read_text(encoding="utf-8")
    try:
        if bruto:
            dados = json.loads(bruto)
        elif os.environ.get("APP_SENHA"):  # modo antigo: um único administrador
            dados = {os.environ.get("APP_USUARIO", "admin"): {"senha": os.environ["APP_SENHA"], "papel": "admin"}}
        else:
            dados = {}
    except ValueError as e:
        print(f"Aviso: USUARIOS não é um JSON válido ({e}). Ninguém conseguirá entrar.")
        return {}
    usuarios = {}
    for login, d in dados.items():
        chave = login.strip().lower()
        usuarios[chave] = {
            "chave": chave,
            "nome": d.get("nome") or login,
            "senha": str(d.get("senha", "")),
            "papel": "admin" if d.get("papel") == "admin" else "cliente",  # padrão: o menor privilégio
            "obras": [str(o) for o in d.get("obras", [])],
        }
        if usuarios[chave]["papel"] == "cliente" and not usuarios[chave]["obras"]:
            print(f"Aviso: o cliente '{login}' não tem 'obras' e não verá nada.")
    return usuarios


# Carregado uma vez ao iniciar. Para mudar usuários é preciso reiniciar (local) ou fazer novo
# deploy (Vercel).
USUARIOS = _carregar_usuarios()
# LOCAL_ADMIN: usado quando não há usuários cadastrados e o programa roda no computador (entra
# direto como admin).
# SEM_ACESSO: perfil mínimo, sem nenhuma obra.
LOCAL_ADMIN = {"chave": "local", "nome": "Administrador", "papel": "admin", "obras": []}
SEM_ACESSO = {"chave": "", "nome": "", "papel": "cliente", "obras": []}

# Chave que assina o cookie de sessão. Na Vercel, se SECRET_KEY faltar, deriva dos usuários
# (estável entre instâncias);
# no computador é aleatória a cada execução.
server.secret_key = os.environ.get("SECRET_KEY") or (
    hashlib.sha256(json.dumps(USUARIOS, sort_keys=True).encode()).hexdigest() if NA_VERCEL else os.urandom(24).hex()
)
# Cookie de sessão: HttpOnly (JavaScript não lê), SameSite=Lax (barra requisições de outros
# sites),
# Secure na Vercel (só HTTPS) e validade de 12 horas.
server.config.update(
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=NA_VERCEL, PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


def usuario_atual():
    """Usuário da sessão. Sem nenhum usuário cadastrado, o uso local é como administrador."""
    if not USUARIOS:
        return None if NA_VERCEL else LOCAL_ADMIN
    # session['u'] guarda a chave do usuário logado (gravada no login).
    return USUARIOS.get(session.get("u")) if has_request_context() else None


def e_admin(u):
    """True se o usuário existe e tem papel de administrador."""
    return bool(u) and u["papel"] == "admin"


def obra_permitida(u, id_obra):
    """Diz se o usuário pode ver a obra. Admin vê todas; cliente vê as que são iguais a um
    item de 'obras' ou começam com ele seguido de espaço."""
    if e_admin(u):
        return True
    o = id_obra.lower()
    # "OBRA-01" libera "OBRA-01 - Reforma Casa Silva", mas não "OBRA-010"
    return any(o == x.lower() or o.startswith(x.lower() + " ") for x in (u or SEM_ACESSO)["obras"])


def visivel(dados, u):
    """Filtra a lista de lançamentos, deixando só o que o usuário pode ver."""
    return dados if e_admin(u) else [d for d in dados if obra_permitida(u, d["ID_Obra"])]


def _senha_ok(guardada, digitada):
    """Confere a senha. Aceita hash (scrypt/pbkdf2) ou texto puro, sempre com comparação em
    tempo constante."""
    if guardada.startswith(("pbkdf2:", "scrypt:")):  # senha guardada como hash (recomendado)
        return check_password_hash(guardada, digitada)
    return hmac.compare_digest(guardada.encode(), digitada.encode())


# Página de login (HTML simples, fora do Dash). _CSS_ e _ERRO_ são trocados ao servir a página.
LOGIN_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrar - Controle de Obras</title><style>_CSS_</style></head>
<body class="login-body"><main class="login">
<div class="brand"><div class="brand__logo">CO</div><div><div class="brand__name">Controle de Obras</div><div class="brand__sub">Planejado x Realizado</div></div></div>
<h1>Entrar</h1>_ERRO_
<form method="post">
<div class="field"><label for="usuario">Usuário</label><input class="input" id="usuario" name="usuario" autocomplete="username" autofocus required></div>
<div class="field"><label for="senha">Senha</label><input class="input" id="senha" name="senha" type="password" autocomplete="current-password" required></div>
<button class="btn btn--accent" type="submit">Entrar</button>
</form></main></body></html>"""


# Roda ANTES de toda requisição: é o 'porteiro' do site.
@server.before_request
def _proteger():
    """Libera /login, /logout e /saude. Na Vercel sem usuários, responde 503. Quem está
    logado passa; chamadas internas do Dash sem sessão recebem 401; o resto vai para
    /login."""
    if request.path in ("/login", "/logout", "/saude"):
        return None
    if not USUARIOS and NA_VERCEL:
        return Response("Configure a variável de ambiente USUARIOS (veja o LEIA-ME).", 503)
    if usuario_atual():
        return None
    if request.path.startswith("/_dash"):
        return Response("Sessão expirada. Recarregue a página.", 401)
    return redirect("/login")


@server.route("/login", methods=["GET", "POST"])
def login():
    """GET mostra o formulário. POST confere usuário e senha, cria a sessão e vai para '/'.
    Se errar, espera 0,8 s e mostra a mensagem."""
    if not USUARIOS or (request.method == "GET" and usuario_atual()):
        return redirect("/")
    erro = ""
    if request.method == "POST":
        u = USUARIOS.get(request.form.get("usuario", "").strip().lower())
        if u and u["senha"] and _senha_ok(u["senha"], request.form.get("senha", "")):
            # limpa qualquer sessão anterior antes de gravar o novo usuário
            session.clear()
            session["u"], session.permanent = u["chave"], True
            return redirect("/")
        time.sleep(0.8)  # atrasa tentativas de adivinhar a senha
        erro = '<p class="login__erro">Usuário ou senha incorretos.</p>'
    return Response(LOGIN_HTML.replace("_CSS_", CSS_ESTILO).replace("_ERRO_", erro), mimetype="text/html")


@server.route("/saude")
def saude():
    """Diagnóstico público e sem dados sensíveis: abra /saude para ver como o servidor está."""
    return {
        "ok": True,
        "caminho_visto_pelo_servidor": request.path,
        "na_vercel": NA_VERCEL,
        "usuarios_carregados": len(USUARIOS),
        "banco": STORE.nome,
        "python": sys.version.split()[0],
    }


@server.route("/logout")
def logout():
    """Encerra a sessão e volta para o login."""
    session.clear()
    return redirect("/login")


# Formato de moeda brasileiro para a tabela do Dash: R$ 1.234,56.
BRL = {"locale": {"symbol": ["R$ ", ""], "decimal": ",", "group": ".", "grouping": [3]}}
# Configuração de coluna 'numérica em reais', reutilizada nas colunas de valores.
moeda = {"type": "numeric", "format": {"specifier": "$,.2f", **BRL}}

# Todas as colunas da tabela (visão do administrador). 'id' = chave do dicionário criado em
# carregar_tabela().
COLUNAS = [
    {"name": "ID_Lancamento", "id": "ID_Lancamento"},
    {"name": "ID_Obra", "id": "ID_Obra"},
    {"name": "ITENS", "id": "ITENS"},
    {"name": "QTD P", "id": "QTD P", "type": "numeric"},
    {"name": "PLANEJADO", "id": "PLANEJADO", **moeda},
    {"name": "QTD R", "id": "QTD R", "type": "numeric"},
    {"name": "REALIZADO", "id": "REALIZADO", **moeda},
    {"name": "DIFERENÇA", "id": "DIFERENÇA", **moeda},
    {"name": "TOTAL PLANEJADO", "id": "TOTAL PLANEJADO", **moeda},
    {"name": "TOTAL REALIZADO", "id": "TOTAL REALIZADO", **moeda},
    {"name": "DIF. TOTAL", "id": "DIF. TOTAL", **moeda},
]

# Colunas que o CLIENTE pode ver: sem os valores unitários (PLANEJADO, REALIZADO e DIFERENÇA).
COLS_CLIENTE = ("ID_Lancamento", "ID_Obra", "ITENS", "QTD P", "QTD R", "TOTAL PLANEJADO", "TOTAL REALIZADO", "DIF. TOTAL")


def colunas_para(u):
    """O cliente não vê os valores unitários, só quantidades e totais."""
    return COLUNAS if e_admin(u) else [c for c in COLUNAS if c["id"] in COLS_CLIENTE]


# Endereço -> qual tela mostrar. As três telas existem na mesma página; navegar() só mostra e
# esconde.
PAGINAS = {"/": "dash", "/lancamentos": "lanc", "/novo": "novo"}
CHAVES = ("dash", "lanc", "novo")  # nomes usados nos ids view-<chave> e nav-<chave>
GRAFICO_CFG = {"displayModeBar": False, "responsive": True}  # esconde a barra de ferramentas do Plotly e deixa o gráfico responsivo


def campo(label, componente, classe=""):
    """Um campo de formulário: rótulo + componente de entrada."""
    return html.Div(className=f"field {classe}".strip(), children=[html.Label(label), componente])


def navlink(texto, icone, href, id_, oculto=False):
    """Link do menu lateral. 'oculto' esconde o item (usado para esconder 'Novo lançamento'
    do cliente)."""
    return dcc.Link([html.Span(icone, className="nav__icon"), html.Span(texto)], href=href, id=id_, className="nav__link", style={"display": "none"} if oculto else None)


def toast(texto, tipo="ok"):
    """Aviso temporário no canto da tela: tipo 'ok' (sucesso) ou 'err' (erro). Some sozinho
    por animação CSS."""
    # o id muda a cada aviso, para a animação reiniciar mesmo com texto repetido
    return html.Span(texto, id=f"t{time.time_ns()}", className=f"msg msg--{tipo}")


def montar_sidebar(u):
    """Menu lateral: marca, links, usuário logado (com 'Sair') e, só para o administrador, o
    banco em uso."""
    admin = e_admin(u)
    rodape = []
    if USUARIOS:
        rodape.append(
            html.Div(
                className="userbox",
                children=[
                    html.Div(u["nome"], className="userbox__nome"),
                    html.Div("Administrador" if admin else "Cliente", className="userbox__papel"),
                    html.A("Sair", href="/logout", className="userbox__sair"),
                ],
            )
        )
    if admin:  # o cliente não precisa saber qual banco está em uso
        rodape.append(html.Div([html.Span(className="dot dot--warn" if STORE.temporario else "dot"), html.Span(STORE.nome)], className="sidebar__foot"))
    return html.Aside(
        className="sidebar",
        children=[
            html.Div(
                className="brand",
                children=[
                    html.Div("CO", className="brand__logo"),
                    html.Div([html.Div("Controle de Obras", className="brand__name"), html.Div("Planejado x Realizado", className="brand__sub")]),
                ],
            ),
            html.Nav(
                className="nav",
                children=[
                    navlink("Dashboard" if admin else "Andamento", "◫", "/", "nav-dash"),
                    navlink("Lançamentos", "≡", "/lancamentos", "nav-lanc"),
                    navlink("Novo lançamento", "+", "/novo", "nav-novo", oculto=not admin),
                ],
            ),
            html.Div(className="sidebar__bottom", children=rodape),
        ],
    )


# Barra superior: filtro de obra, busca e botão de exportar. Os valores alimentam o callback
# renderizar().
topbar = html.Header(
    className="topbar",
    children=[
        html.Div(
            className="topbar__filters",
            children=[
                html.Div(dcc.Dropdown(id="f_obra", placeholder="Todas as obras", clearable=True), className="filter filter--obra"),
                dcc.Input(id="f_busca", type="text", placeholder="Buscar item, lançamento ou obra…", debounce=False, className="input filter filter--busca"),
            ],
        ),
        html.Button("Exportar Excel", id="btn_xls", n_clicks=0, className="btn btn--ghost"),
    ],
)


def grafico(id_, altura):
    """Cria um gráfico (dcc.Graph) vazio com id e altura. As figuras chegam pelo callback
    renderizar()."""
    return dcc.Graph(id=id_, config=GRAFICO_CFG, style={"height": f"{altura}px"})


def montar_dash(u):
    """Tela do Dashboard: velocímetro + indicadores, gráfico por obra, rosca e desvios por
    item. O título muda conforme o perfil."""
    admin = e_admin(u)
    cabecalho = [html.H1("Dashboard" if admin else "Andamento da obra", className="page-title")]
    if not admin:
        cabecalho.append(html.Div(f"Olá, {u['nome']}. Este é o andamento financeiro da sua obra.", className="page-sub"))
    return html.Section(
        id="view-dash",
        children=[
            *cabecalho,
            html.Div(
                className="hero",
                children=[
                    html.Div(className="card", children=[html.H3("Orçamento executado", className="card__title"), grafico("g_gauge", 230), html.Div(id="resumo")]),
                    html.Div(id="kpis", className="kpis"),
                ],
            ),
            html.Div(
                className="charts",
                children=[
                    html.Div(className="card", children=[html.H3("Planejado x Realizado por obra", className="card__title"), grafico("g_obra", 340)]),
                    html.Div(className="card", children=[html.H3("Para onde foi o dinheiro", className="card__title"), grafico("g_donut", 340)]),
                ],
            ),
            html.Div(
                className="card",
                children=[
                    html.H3("Maiores desvios por item", className="card__title"),
                    html.Div("Vermelho: estouro • Verde: economia", className="card__hint"),
                    grafico("g_item", 340),
                ],
            ),
        ],
    )


def montar_lanc(u):
    """Tela de Lançamentos: tabela (colunas e seleção dependem do perfil) e botão de excluir
    (só admin)."""
    admin = e_admin(u)
    return html.Section(
        id="view-lanc",
        children=[
            html.H1("Lançamentos", className="page-title"),
            html.Div(
                className="card",
                children=[
                    html.Div(
                        className="card__head",
                        children=[
                            html.H3("Tabela de lançamentos", className="card__title"),
                            html.Button("Excluir selecionados", id="btn_del", n_clicks=0, className="btn btn--danger", style=None if admin else {"display": "none"}),
                        ],
                    ),
                    # data=[] começa vazio e é preenchido por renderizar(). Filtro e ordenação
                    # nativos, 15 linhas por página;
                    # cores condicionais destacam estouro (vermelho) e economia (verde).
                    dash_table.DataTable(
                        id="tabela", columns=colunas_para(u), data=[], row_selectable="multi" if admin else False, selected_rows=[],
                        page_size=15, filter_action="native", sort_action="native",
                        style_table={"overflowX": "auto"},
                        style_header={"backgroundColor": INK, "color": "white", "fontWeight": "600", "border": "none", "fontSize": "12.5px", "fontFamily": FONTE},
                        style_filter={"backgroundColor": "#F4F7F6"},
                        style_cell={"padding": "11px 12px", "textAlign": "left", "fontFamily": FONTE, "fontSize": "13px", "border": "none", "borderBottom": f"1px solid {GRID}", "minWidth": "90px"},
                        style_data_conditional=[
                            {"if": {"row_index": "odd"}, "backgroundColor": "#F7F9F8"},
                            {"if": {"filter_query": "{DIFERENÇA} > 0", "column_id": "DIFERENÇA"}, "color": BAD, "fontWeight": "600"},
                            {"if": {"filter_query": "{DIFERENÇA} < 0", "column_id": "DIFERENÇA"}, "color": GOOD, "fontWeight": "600"},
                            {"if": {"filter_query": "{DIF. TOTAL} > 0", "column_id": "DIF. TOTAL"}, "color": BAD, "fontWeight": "700"},
                            {"if": {"filter_query": "{DIF. TOTAL} < 0", "column_id": "DIF. TOTAL"}, "color": GOOD, "fontWeight": "700"},
                        ],
                    ),
                ],
            ),
        ],
    )


# Tela 'Novo lançamento' (formulário). Existe no layout para todos, mas é escondida e bloqueada
# para o cliente.
# Os ids (id_lanc, id_obra, item, ...) são lidos pelo callback atualizar().
view_novo = html.Section(
    id="view-novo",
    children=[
        html.H1("Novo lançamento", className="page-title"),
        html.Div(
            className="card",
            children=[
                html.H3("Cadastro de item", className="card__title"),
                html.Div(
                    className="form",
                    children=[
                        campo("ID_Lancamento", dcc.Input(id="id_lanc", placeholder="ex: LAN-001", className="input")),
                        campo("ID_Obra", dcc.Input(id="id_obra", placeholder="ex: OBRA-01 - Reforma Casa Silva", className="input"), "field--wide"),
                        campo("Item", dcc.Input(id="item", placeholder="Descrição do item", className="input"), "field--wide"),
                        campo("QTD P", dcc.Input(id="qtd_p", type="number", min=0, step="any", placeholder="0", className="input")),
                        campo("Planejado (unit.)", dcc.Input(id="planejado", type="number", min=0, step="any", placeholder="0,00", className="input")),
                        campo("QTD R", dcc.Input(id="qtd_r", type="number", min=0, step="any", placeholder="0", className="input")),
                        campo("Realizado (unit.)", dcc.Input(id="realizado", type="number", min=0, step="any", placeholder="0,00", className="input")),
                    ],
                ),
                html.Div(id="previa", className="previa"),
                html.Div(className="form__actions", children=[html.Button("Inserir lançamento", id="btn_add", n_clicks=0, className="btn btn--accent")]),
                html.Div("O ID_Lancamento deve ser único. Depois de inserir, só o ID_Obra é mantido para facilitar o próximo item.", className="card__hint"),
            ],
        ),
    ],
)


def servir_layout():
    """Montado a cada carregamento de página, de acordo com o perfil de quem entrou."""
    u = usuario_atual() or SEM_ACESSO  # sem sessão: o menor privilégio
    return html.Div(
        className="app",
        children=[
            dcc.Location(id="url"),  # lê o endereço do navegador
            dcc.Store(id="refresh", data=0),  # contador: muda a cada inserção/exclusão para forçar nova leitura
            dcc.Download(id="download"),  # entrega o arquivo Excel ao navegador
            html.Div(id="dummy", style={"display": "none"}),  # destino de um callback que só executa JavaScript
            montar_sidebar(u),
            html.Main(
                className="main",
                children=[topbar, html.Div(id="msg", className="toast-wrap"), montar_dash(u), montar_lanc(u), view_novo],
            ),
        ],
    )


# Passar a FUNÇÃO (sem parênteses) faz o Dash montar a tela a cada carregamento da página:
# por isso cada perfil vê um layout diferente.
app.layout = servir_layout


# ------------------------------------------------------------------ CALLBACKS
# Callbacks: funções que o Dash chama quando algo acontece na tela.
# Input = o que dispara; State = valor lido sem disparar; Output = o que é atualizado.
@app.callback(
    *[Output(f"view-{k}", "style") for k in CHAVES],
    *[Output(f"nav-{k}", "className") for k in CHAVES],
    Input("url", "pathname"),
)
def navegar(path):
    """Mostra só a tela do endereço atual e destaca o link ativo. Cliente que abrir /novo
    volta ao dashboard."""
    atual = PAGINAS.get(path or "/", "dash")
    if atual == "novo" and not e_admin(usuario_atual()):
        atual = "dash"
    estilos = [{} if k == atual else {"display": "none"} for k in CHAVES]
    classes = ["nav__link nav__link--active" if k == atual else "nav__link" for k in CHAVES]
    return (*estilos, *classes)


# Gráficos em seções ocultas podem nascer com tamanho errado: força o Plotly a recalcular.
clientside_callback(
    """
    function(path) {
        setTimeout(function () { window.dispatchEvent(new Event('resize')); }, 60);
        return window.dash_clientside.no_update;
    }
    """,
    Output("dummy", "children"),
    Input("url", "pathname"),
)

# Usado nos retornos de erro de atualizar(): 6 x no_update = 'não mexa nos campos do formulário'.
LIMPAR_CAMPOS = (no_update,) * 6  # id_lanc, item, qtd_p, planejado, qtd_r, realizado


@app.callback(
    Output("refresh", "data"),
    Output("msg", "children"),
    Output("id_lanc", "value"),
    Output("item", "value"),
    Output("qtd_p", "value"),
    Output("planejado", "value"),
    Output("qtd_r", "value"),
    Output("realizado", "value"),
    Input("btn_add", "n_clicks"),
    Input("btn_del", "n_clicks"),
    State("id_lanc", "value"),
    State("id_obra", "value"),
    State("item", "value"),
    State("qtd_p", "value"),
    State("planejado", "value"),
    State("qtd_r", "value"),
    State("realizado", "value"),
    State("tabela", "selected_rows"),
    State("tabela", "data"),
    State("refresh", "data"),
    prevent_initial_call=True,
)
def atualizar(_add, _del, id_lanc, id_obra, item, qtd_p, plan, qtd_r, real, sel, dados, cont):
    """Botões Inserir e Excluir (só admin). Fluxo: checa permissão; se for exclusão, apaga as
    linhas marcadas; senão valida, insere e limpa o formulário (menos o ID_Obra). Retorna:
    contador, aviso e os 6 campos."""
    if not e_admin(usuario_atual()):  # regra valendo no servidor, não só na tela
        return no_update, toast("Você não tem permissão para alterar lançamentos.", "err"), *LIMPAR_CAMPOS
    # novo valor do contador 'refresh': faz renderizar() recarregar tudo.
    cont = (cont or 0) + 1

    # ctx.triggered_id diz qual botão foi clicado.
    if ctx.triggered_id == "btn_del":
        if not sel:
            return no_update, toast("Selecione ao menos um lançamento para excluir.", "err"), *LIMPAR_CAMPOS
        try:
            # sel = posições marcadas na tabela; dados[i]['id'] = id do registro no banco.
            STORE.excluir([dados[i]["id"] for i in sel])
        except Exception as e:  # noqa: BLE001
            return no_update, toast(f"Não foi possível excluir: {erro_curto(e)}", "err"), *LIMPAR_CAMPOS
        invalidar_cache()
        return cont, toast(f"{len(sel)} lançamento(s) excluído(s)."), *LIMPAR_CAMPOS

    try:
        erro = validar_lancamento(id_lanc, id_obra, item, qtd_p, plan, qtd_r, real)
    except Exception as e:  # noqa: BLE001  (ex.: Firestore não criado ou sem permissão)
        return no_update, toast(f"Não consegui acessar o banco de dados: {erro_curto(e)}", "err"), *LIMPAR_CAMPOS
    if erro:
        return no_update, toast(erro, "err"), *LIMPAR_CAMPOS
    try:
        # strip() remove espaços das pontas; os números viram float.
        STORE.inserir(id_lanc.strip(), id_obra.strip(), item.strip(), float(qtd_p), float(plan), float(qtd_r), float(real))
    except Duplicado:
        return no_update, toast("ID_Lancamento duplicado. Use um ID diferente.", "err"), *LIMPAR_CAMPOS
    except Exception as e:  # noqa: BLE001  (ex.: Firebase fora do ar)
        return no_update, toast(f"Não foi possível salvar: {erro_curto(e)}", "err"), *LIMPAR_CAMPOS
    invalidar_cache()
    # sucesso: None limpa os 6 campos; o ID_Obra fica (não é saída deste callback).
    return cont, toast("Lançamento inserido com sucesso."), None, None, None, None, None, None


@app.callback(
    Output("previa", "children"),
    Input("qtd_p", "value"), Input("planejado", "value"), Input("qtd_r", "value"), Input("realizado", "value"),
)
def previa(qp, pl, qr, rl):
    """Atualiza em tempo real os totais do formulário enquanto o admin digita. Não grava
    nada."""
    def n(v):
        return float(v) if isinstance(v, (int, float)) else 0.0

    tp, tr = n(qp) * n(pl), n(qr) * n(rl)
    dif = tr - tp
    tom = "bad" if dif > 0 else "good" if dif < 0 else ""

    def bloco(rotulo, valor, cls=""):
        return html.Div([html.Div(rotulo, className="previa__label"), html.Div(valor, className=f"previa__value {cls}".strip())])

    return [bloco("Total planejado", brl(tp)), bloco("Total realizado", brl(tr)), bloco("Diferença", brl(dif), tom)]


@app.callback(
    Output("kpis", "children"),
    Output("resumo", "children"),
    Output("g_gauge", "figure"),
    Output("g_obra", "figure"),
    Output("g_donut", "figure"),
    Output("g_item", "figure"),
    Output("tabela", "data"),
    Output("f_obra", "options"),
    Output("tabela", "selected_rows"),
    Input("refresh", "data"),
    Input("f_obra", "value"),
    Input("f_busca", "value"),
)
def renderizar(_refresh, obra, busca):
    """Callback central de leitura. Roda ao abrir a página, depois de inserir/excluir e ao
    mudar filtro ou busca: carrega os dados visíveis ao usuário, aplica os filtros e
    devolve indicadores, gráficos, tabela e opções do filtro de obras. Se o banco falhar,
    mostra o erro no painel."""
    falha = None
    try:
        base = visivel(carregar_tabela(), usuario_atual())
    except Exception as e:  # noqa: BLE001
        print("Erro ao ler os dados:", e)
        base, falha = [], erro_curto(e)
    # opções do filtro de obras: só as obras que o usuário pode ver.
    opcoes = [{"label": o, "value": o} for o in sorted({d["ID_Obra"] for d in base})]
    dados = filtrar(base, obra, busca)
    return (
        montar_kpis(dados),
        html.Div(f"Erro ao ler o banco de dados: {falha}", className="hero__sub") if falha else montar_resumo(dados),
        fig_gauge(dados), fig_obras(dados),
        fig_donut(dados), fig_itens(dados), dados, opcoes, [],
    )


@app.callback(Output("download", "data"), Input("btn_xls", "n_clicks"), prevent_initial_call=True)
def exportar(_):
    """Gera o Excel (.xlsx) com as colunas e linhas que o usuário pode ver, com cabeçalho
    estilizado, filtro, linha congelada e formato de moeda."""
    from openpyxl import Workbook  # só é necessário para exportar
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Lançamentos"
    u = usuario_atual()
    ids = [c["id"] for c in colunas_para(u)]
    ws.append(ids)
    for d in visivel(carregar_tabela(), u):
        ws.append([d[i] for i in ids])

    moeda_cols = {"PLANEJADO", "REALIZADO", "DIFERENÇA", "TOTAL PLANEJADO", "TOTAL REALIZADO", "DIF. TOTAL"}
    for idx, nome in enumerate(ids, start=1):
        cab = ws.cell(row=1, column=idx)
        cab.fill, cab.font = PatternFill("solid", fgColor=INK.lstrip("#")), Font(bold=True, color="FFFFFF")
        cab.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[cab.column_letter].width = max(15, len(nome) + 4)
        if nome in moeda_cols:
            for linha in range(2, ws.max_row + 1):
                ws.cell(row=linha, column=idx).number_format = '"R$" #,##0.00'
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return dcc.send_bytes(wb.save, "controle_obras.xlsx")


# Só roda ao executar 'python controle_obras.py' no computador.
# Na Vercel o arquivo é apenas importado (index.py) e este bloco não executa.
if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 8050))
    url = f"http://127.0.0.1:{porta}"
    print(f"\n  Controle de Obras rodando em {url}  (dados: {STORE.nome})\n  Para parar: Ctrl+C\n")
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()  # espera 1,5 s e abre o navegador
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=porta, debug=False)
