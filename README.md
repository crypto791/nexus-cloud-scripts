# Nexus Cloud — Cloud Studio

## Instalação
1. Suba todos os arquivos para a raiz do repositório, com `editor.html` ao lado de `main.py`.
2. No Supabase, execute `schema.sql` no SQL Editor.
3. No Render, use `render.yaml` como Blueprint e configure `DATABASE_URL`, `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`. O `SESSION_SECRET` é gerado pelo Blueprint.
4. Acesse `/login`, crie sua conta, faça login, crie um projeto, salve e gere um ticket.
5. Abra `Executor.client.lua` em um ambiente autorizado, cole a URL do ticket e execute antes de 60 segundos.

## Segurança
Não publique tokens, URLs com senha, nem SESSION_SECRET no GitHub. O ticket é uma credencial temporária de acesso ao código; não compartilhe. O status confirmado indica que o cliente reportou conclusão, não uma comprovação independente. O envio ao Telegram acontece ao salvar uma edição de projeto com código.
