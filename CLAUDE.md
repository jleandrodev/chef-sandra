# Chef Sandra — instruções para o assistente

## Fluxo Git (regra durável)

Toda alteração feita aqui deve ser **commitada e pushada** pro remoto antes de
encerrar a tarefa. Não acumule mudanças locais entre conversas.

- Repositório remoto: `git@github.com:jleandrodev/chef-sandra.git` (SSH)
- Branch principal: `main` com tracking pra `origin/main`
- Mensagens de commit em português, no padrão do repo (1ª linha curta + corpo
  explicando o **porquê**, não o quê).
- **Nunca commite secrets.** `OPENAI_API_KEY` e qualquer outro segredo vão no
  `.env` (gitignored). `agent.py` carrega via `_load_env_file()` no boot.
- `.env.example` documenta as variáveis esperadas — manter atualizado quando
  novas variáveis forem introduzidas.
- `evolution-api/` é dependência upstream (clone do repo público da Evolution
  API) e está ignorada — não versionar aqui.

### Sequência padrão após mudar arquivo

```bash
cd /root/chef-sandra
git add <arquivos>
git commit -m "<mensagem clara do porquê>"
git push
```

Se houve mudança em outra máquina/colaborador antes: `git pull --rebase` antes
do push.

## Operacional

- Rodando sob PM2 com nomes `watcher` (cwd `/root/chef-sandra`, script
  `watcher.py`) e `evolution-api` (cwd `/root/chef-sandra/evolution-api`,
  `npm run start`). Após mudar config: `pm2 restart watcher` e/ou
  `pm2 restart evolution-api` (PATH precisa incluir
  `/root/.nvm/versions/node/v24.14.0/bin`).
- `INSTANCE_NAME = "meu-agente"` em `watcher.py` e `recover.py` é o ID da
  sessão WhatsApp no Evolution API. **Não trocar** sem combinar antes — mudar
  desconecta a sessão e exige novo QR code. O nome do projeto é "Chef Sandra"
  mas a instância segue como "meu-agente" por compatibilidade.
- Logs: `pm2 logs watcher --lines 50 --nostream`.
