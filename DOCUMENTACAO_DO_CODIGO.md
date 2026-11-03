# Documentação do código — Controle de Obras

Este arquivo explica **o que foi feito** e **quais funções existem**, no back-end e no front-end.
O código-fonte (`controle_obras.py`) tem os mesmos comentários e docstrings, na ordem em que aparecem aqui.

---

## 1. O que foi feito

| Etapa | O que mudou |
|---|---|
| Ponto de partida | Um arquivo Dash com SQLite e gráficos de colunas. Rodava só localmente e dependia de instalar as bibliotecas à mão (erro `No module named 'plotly'`). |
| Execução mais fácil | O programa instala o que faltar sozinho; `iniciar.bat` cria o ambiente e abre o navegador; o banco `obras.db` fica ao lado do arquivo; bancos de versões antigas viram backup em vez de travar o programa. |
| Visual novo | Novo CSS (cores, fontes, cartões, menu lateral). Os gráficos de colunas foram trocados por velocímetro, "halteres", rosca e "pirulitos". |
| Banco na nuvem | Camada de dados com duas opções de mesma interface: SQLite (local) e Firebase Firestore (nuvem). |
| Publicação | `index.py` para a Vercel, regras do Firestore, variáveis de ambiente e modo demonstração quando não há Firebase. |
| Perfis de acesso | Login por usuário e senha, com perfis **administrador** e **cliente**. O cliente só lê as obras liberadas para ele. |
| Melhorias de uso | Prévia dos totais ao digitar, avisos que somem sozinhos, exportação do Excel estilizada e cache para economizar leituras no Firestore. |
| Diagnóstico | Página `/saude` e erros do banco mostrados na tela (antes falhavam em silêncio). |

---

## 2. Como as partes se conectam

```
Navegador
   │  (HTTPS)
   ▼
Vercel ── Flask ── porteiro (_proteger) ── login/logout/saude
   │
   └─ Dash ── layout por perfil (servir_layout)
          └─ callbacks ──► STORE (SQLiteStore ou FirestoreStore)
                              └─ Firestore (nuvem)  ou  obras.db (local)
```

Ordem do arquivo `controle_obras.py`:

1. Ambiente (`.env`, instalação automática)
2. Back-end (bancos, cache, validação)
3. Helpers de front (formatação, indicadores, gráficos)
4. Front (CSS, modelo HTML, criação do app)
5. Acesso (usuários, login, permissões)
6. Telas (menu, dashboard, lançamentos, formulário)
7. Callbacks (ações da tela)
8. Execução (servidor local)

---

## 3. Back-end

### 3.1 Ambiente

| Função | O que faz |
|---|---|
| `_carregar_env()` | Lê o arquivo `.env`, se existir, e coloca as linhas nas variáveis de ambiente (sem precisar do `python-dotenv`). |
| `_garantir_dependencias()` | No computador, instala `dash`, `plotly` e `openpyxl` se faltarem. Na Vercel não faz nada (usa o `requirements.txt`). |

### 3.2 Bancos de dados

Os dois bancos têm os mesmos quatro métodos. O resto do código usa só `STORE`.

| Classe / função | O que faz |
|---|---|
| `Duplicado` | Erro próprio: o ID_Lancamento já existe. |
| `SQLiteStore` | Banco local em arquivo. `_con()` abre e **sempre fecha** a conexão; `_preparar()` cria a tabela, confere as colunas (faz backup se for versão antiga) e cria um índice único sem diferenciar maiúsculas. |
| `FirestoreStore` | Banco na nuvem. Cada lançamento é um documento; `_doc_id()` usa o ID_Lancamento (minúsculo, com prefixo `l_`) como ID do documento, e `create()` falha se ele já existir. |
| `listar()` | Todos os lançamentos, na ordem de cadastro. |
| `existe(id)` | Diz se o ID já está em uso. |
| `inserir(...)` | Grava um lançamento; levanta `Duplicado` se o ID já existir. |
| `excluir(ids)` | Apaga os lançamentos informados (no Firestore, em lotes de até 400). |
| `_credenciais_firebase()` | Lê a chave de serviço: variável `FIREBASE_CREDENTIALS` (JSON ou base64) ou arquivo `serviceAccountKey.json`. |
| `criar_store()` | Escolhe o banco: Firestore se houver credenciais; senão SQLite (em `/tmp` na Vercel, marcado como "demonstração"). |

### 3.3 Dados e regras

| Função | O que faz |
|---|---|
| `carregar_tabela()` | Lê os lançamentos e calcula `TOTAL PLANEJADO`, `TOTAL REALIZADO`, `DIFERENÇA` e `DIF. TOTAL`. Usa cache (0 s no SQLite, 15 s no Firestore). |
| `invalidar_cache()` | Descarta o cache depois de inserir ou excluir. |
| `erro_curto(e)` | Resume mensagens de erro longas para o aviso da tela. |
| `validar_lancamento(...)` | Confere: campos preenchidos, números válidos, sem negativos e ID único. Devolve a mensagem de erro ou `None`. |

### 3.4 Acesso e segurança

| Função / rota | O que faz |
|---|---|
| `_carregar_usuarios()` | Lê `USUARIOS` (variável) ou `usuarios.json`. Papel padrão: `cliente`. |
| `usuario_atual()` | Descobre o usuário pela sessão. Sem usuários cadastrados e rodando no computador, entra como administrador. |
| `e_admin(u)` | Diz se o usuário é administrador. |
| `obra_permitida(u, obra)` | `OBRA-01` libera `OBRA-01 - Reforma...`, mas não `OBRA-010`. |
| `visivel(dados, u)` | Filtra os lançamentos que o usuário pode ver. |
| `colunas_para(u)` | O cliente não vê os valores unitários. |
| `_senha_ok(guardada, digitada)` | Aceita hash (`scrypt:`/`pbkdf2:`) ou texto puro, com comparação em tempo constante. |
| `_proteger()` | "Porteiro" executado antes de toda requisição: libera `/login`, `/logout` e `/saude`; bloqueia quem não entrou. |
| `/login`, `/logout` | Entrada e saída; a sessão dura 12 horas. Senha errada gera um atraso de 0,8 s. |
| `/saude` | Diagnóstico público, sem dados sensíveis. |

---

## 4. Front-end

### 4.1 Formatação e indicadores

| Função | O que faz |
|---|---|
| `brl(v)` | Formata como moeda: `1234.5` vira `R$ 1.234,50`. |
| `curto(texto, n)` | Encurta nomes longos com "…". |
| `filtrar(dados, obra, busca)` | Aplica o filtro de obra e a busca por texto. |
| `totais(dados)` | Devolve (total planejado, total realizado). |
| `kpi(...)` | Monta um cartão de indicador. |
| `montar_kpis(dados)` | Os 4 cartões: Planejado, Realizado, Desvio e Lançamentos. |
| `montar_resumo(dados)` | Frase de destaque (acima ou abaixo do orçamento). |

### 4.2 Gráficos (Plotly)

| Função | Gráfico | Como funciona |
|---|---|---|
| `fig_gauge` | Velocímetro | % executado = realizado ÷ planejado × 100; vermelho acima de 100%. |
| `fig_obras` | "Halteres" | Dois pontos por obra (planejado e realizado) ligados por uma haste; 8 obras de maior gasto. |
| `fig_donut` | Rosca | Divisão do gasto realizado entre as obras; total no centro. |
| `fig_itens` | "Pirulitos" | 8 itens de maior desvio; vermelho = estouro, verde = economia. |
| `fig_vazia` | Mensagem | Aparece quando não há dados. |
| `_base` | Estilo comum | Fundo transparente, fonte, legenda, hover e separadores brasileiros. |

### 4.3 Telas e componentes

| Função / objeto | O que faz |
|---|---|
| `CSS_ESTILO`, `INDEX_HTML` | Aparência do sistema e modelo da página; o CSS é injetado no HTML do Dash. |
| `LOGIN_HTML` | Página de login (HTML simples, fora do Dash). |
| `servir_layout()` | Monta a tela a cada carregamento, de acordo com o perfil. |
| `montar_sidebar(u)` | Menu lateral, usuário logado e botão Sair. |
| `topbar` | Filtro de obra, busca e botão de exportar. |
| `montar_dash(u)` | Tela do Dashboard (título muda para o cliente). |
| `montar_lanc(u)` | Tela da tabela; o botão de excluir e a seleção de linhas só existem para o administrador. |
| `view_novo` | Formulário de novo lançamento. |
| `campo()`, `navlink()`, `grafico()` | Peças reutilizáveis (campo de formulário, link do menu, gráfico vazio). |
| `toast(texto, tipo)` | Aviso no canto da tela; some sozinho por animação CSS. |
| `COLUNAS`, `COLS_CLIENTE` | Colunas da tabela por perfil. |

---

## 5. Callbacks (ações da tela)

| Callback | Dispara com (Input) | Atualiza (Output) | O que faz |
|---|---|---|---|
| `navegar` | endereço da página | estilo das 3 telas e classe dos 3 links | Mostra só a tela atual; o cliente em `/novo` volta ao dashboard. |
| JavaScript (clientside) | endereço da página | elemento oculto `dummy` | Dispara um evento de redimensionar para os gráficos ocuparem o espaço certo. |
| `atualizar` | botões Inserir e Excluir | contador `refresh`, aviso e 6 campos | Só administrador. Exclui as linhas marcadas ou valida e insere um lançamento. |
| `previa` | 4 campos numéricos | painel de prévia | Mostra os totais enquanto o administrador digita. Não grava nada. |
| `renderizar` | contador `refresh`, filtro de obra e busca | indicadores, resumo, 4 gráficos, tabela e opções do filtro | Leitura central: carrega os dados visíveis ao usuário e redesenha tudo. |
| `exportar` | botão Exportar Excel | download | Gera o `.xlsx` com o que o usuário pode ver. |

### Fluxo: inserir um lançamento

1. O administrador digita; `previa()` mostra os totais em tempo real.
2. Clica em **Inserir lançamento**; `atualizar()` confere se ele é administrador.
3. `validar_lancamento()` aplica as regras (e consulta o banco para o ID único).
4. `STORE.inserir()` grava; no Firestore, `create()` impede ID repetido.
5. `invalidar_cache()` e o contador `refresh` mudam, então `renderizar()` recarrega.
6. Indicadores, gráficos e tabela são redesenhados, e o formulário é limpo (menos o ID_Obra).

### Fluxo: entrar no sistema

1. Qualquer endereço passa por `_proteger()`. Sem sessão, vai para `/login`.
2. `login()` confere usuário e senha (`_senha_ok`) e grava o usuário na sessão (cookie assinado).
3. `servir_layout()` monta a tela do perfil, e `renderizar()` carrega só as obras permitidas (`visivel`).

---

## 6. Bibliotecas e funções usadas

| Biblioteca | Principais funções e componentes | Para quê |
|---|---|---|
| **Dash** | `Dash`, `html.*` (Div, Span, Section, Aside, Header, Main, Nav, H1, H3, Label, Button, A), `dcc.Graph`, `dcc.Input`, `dcc.Dropdown`, `dcc.Location`, `dcc.Store`, `dcc.Download`, `dcc.Link`, `dcc.send_bytes`, `dash_table.DataTable`, `Input`/`Output`/`State`, `ctx`, `no_update`, `clientside_callback` | Páginas, formulários, tabela e ações da tela |
| **Plotly** | `go.Figure`, `go.Scatter`, `go.Pie`, `go.Indicator`, `add_trace`, `add_annotation`, `update_layout`, `update_xaxes`, `update_yaxes` | Gráficos |
| **Flask / Werkzeug** | `route`, `before_request`, `session`, `redirect`, `request`, `Response`, `has_request_context`, `check_password_hash` | Login, sessão e rotas |
| **sqlite3** | `connect`, `execute`, `executemany`, `Row`, `PRAGMA table_info` | Banco local |
| **firebase-admin** | `initialize_app`, `credentials.Certificate`, `firestore.client`, `collection`, `document`, `create`, `get`, `stream`, `batch`, `commit` | Banco na nuvem |
| **openpyxl** | `Workbook`, `PatternFill`, `Font`, `Alignment`, `number_format`, `freeze_panes`, `auto_filter` | Exportar Excel |
| **Python padrão** | `pathlib`, `json`, `base64`, `hashlib`, `hmac`, `subprocess`, `importlib.util`, `threading`, `webbrowser`, `contextlib`, `time`, `math`, `os`, `sys` | Arquivos, segurança, instalação e execução |

---

## 7. Variáveis de ambiente

| Variável | Para quê |
|---|---|
| `FIREBASE_CREDENTIALS` | JSON da chave de serviço do Firebase (ou em base64). Sem ela, usa SQLite. |
| `FIREBASE_COLLECTION` | Nome da coleção no Firestore (padrão: `lancamentos`). |
| `USUARIOS` | JSON com administrador e clientes (mesmo formato do `usuarios.example.json`). |
| `SECRET_KEY` | Texto longo que assina o cookie de sessão. Obrigatório na Vercel. |
| `APP_USUARIO`, `APP_SENHA` | Modo antigo: um único administrador (só vale se `USUARIOS` não existir). |
| `PORT`, `HOST`, `NO_BROWSER` | Porta, endereço e opção de não abrir o navegador (uso local). |
| `VERCEL` | Definida pela própria Vercel; liga o modo de publicação. |

---

## 8. Sobre os comentários no código

Os comentários e as docstrings foram adicionados **sem mudar a lógica**. A comparação da estrutura do código antes e depois (ignorando comentários e docstrings) confirmou que é idêntica.
