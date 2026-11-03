# Controle de Obras — Planejado x Realizado

Dashboard em Dash com login (administrador e cliente) e dados em SQLite (local) ou Firebase Firestore (nuvem).

## Rodar no seu computador

**Windows, jeito mais fácil:** dois cliques em `iniciar.bat`. Ele cria o ambiente virtual, instala o que falta e abre o navegador.

**Pelo terminal do VS Code:** `python controle_obras.py` (se faltar biblioteca, o arquivo tenta instalar sozinho). Abre em http://127.0.0.1:8050. Sem configuração, os dados ficam em `obras.db`, ao lado do arquivo.

## Administrador x cliente

| | Administrador | Cliente |
|---|---|---|
| Obras que enxerga | todas | só as listadas em `obras` |
| Dashboard e gráficos | sim | sim, só das obras dele |
| Tabela de lançamentos | todas as colunas | só quantidades e totais (sem valores unitários) |
| Novo lançamento / excluir | sim | não (bloqueado também no servidor) |
| Exportar Excel | tudo | só o que ele pode ver |

**Como cadastrar os usuários:** copie `usuarios.example.json` para `usuarios.json` e edite. Em `obras`, `"OBRA-01"` libera `OBRA-01 - Reforma Casa Silva` (e não `OBRA-010`). Se um cliente ficar sem `obras`, ele não vê nada.

**Sem nenhum usuário cadastrado**, o uso local entra direto como administrador, sem login. Na Vercel isso é bloqueado: sem `USUARIOS` o site não abre.

**Senha com hash (recomendado ao publicar):** no lugar da senha em texto, gere um hash e cole no campo `senha`:

```
python -c "from werkzeug.security import generate_password_hash as g; print(g('minha-senha'))"
```

## 1) Firebase (banco de dados na nuvem)

1. Em https://console.firebase.google.com crie um projeto.
2. **Build → Firestore Database → Criar banco de dados** (modo produção; região `southamerica-east1`, São Paulo).
3. **Configurações do projeto → Contas de serviço → Gerar nova chave privada.** Baixa um `.json`.
4. Para testar local com Firebase, escolha um:
   - salve o arquivo como `serviceAccountKey.json` ao lado de `controle_obras.py`; ou
   - copie `.env.example` para `.env` e cole o JSON inteiro (em uma linha) em `FIREBASE_CREDENTIALS`.
   - Local, instale também: `python -m pip install firebase-admin`
5. Publique as regras que bloqueiam acesso direto ao banco (opcional, recomendado):

```
npm install -g firebase-tools
firebase login
firebase use --add
firebase deploy --only firestore:rules
```

O app grava na coleção `lancamentos`, e o ID do documento é o próprio ID_Lancamento, então o banco impede duplicados.

> Nunca envie `serviceAccountKey.json`, `usuarios.json` ou `.env` para o GitHub (o `.gitignore` já os ignora).

## 2) Vercel (publicar o site)

1. Suba a pasta para um repositório no GitHub.
2. Em https://vercel.com → **Add New → Project** → importe o repositório (a Vercel detecta o preset **Python** sozinha; deixe como está).
3. Em **Settings → Environment Variables**, cadastre:
   - `FIREBASE_CREDENTIALS` = conteúdo inteiro do JSON da chave
   - `USUARIOS` = JSON dos usuários, em uma linha (veja `.env.example`)
   - `SECRET_KEY` = texto longo e aleatório (ex.: `python -c "import secrets; print(secrets.token_hex(32))"`)
4. **Deploy.** Pela linha de comando: `npm i -g vercel`, depois `vercel` (teste) e `vercel --prod`.

Sem `FIREBASE_CREDENTIALS`, o site na Vercel entra em *modo demonstração*: grava em disco temporário e **os dados somem**. A barra lateral do administrador mostra qual banco está em uso.

Se o site não abrir, acesse `/saude` no endereço do site: mostra se as variáveis foram lidas e qual banco está em uso.

Para adicionar ou trocar um cliente depois, edite `USUARIOS` no painel da Vercel e faça um novo deploy.

## Arquivos

| Arquivo | Para quê |
|---|---|
| `controle_obras.py` | App completo (back-end, login e front) |
| `iniciar.bat` | Executa tudo com dois cliques (Windows) |
| `usuarios.example.json` | Modelo de usuários (admin e clientes) |
| `DOCUMENTACAO_DO_CODIGO.md` | Explica o que foi feito e cada função (front e back) |
| `index.py` | Ponto de entrada da Vercel (ela detecta sozinha, sem `vercel.json`) |
| `requirements.txt` | Dependências na Vercel |
| `firebase.json`, `firestore.rules`, `firestore.indexes.json` | Configuração do Firestore |
| `.env.example` | Modelo das variáveis de ambiente |
