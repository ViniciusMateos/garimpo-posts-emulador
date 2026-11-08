# Garimpo (emulador)

Acha posts novos pro chat do Auto Follow sozinho e manda no grupo, tocando no app real do Instagram
(uiautomator2) no MuMu Player. Não segue, não curte e não comenta: só olha perfis e envia posts.

Faz parte do [quase-nada-bots-device](https://github.com/ViniciusMateos/quase-nada-bots-device):
fica em `bots/garimpo/` e usa os bots `auto_follow` (folha de curtidas, trocar conta) e
`brecho_scraper` (abrir perfil, grade, feed do perfil).

## Por que existe

A lista de curtidas do Instagram mostra no máximo umas 100 pessoas por post, e os posts do chat eram
todos da mesma turma: quem curte já tinha sido seguido. Cada post rendia ~95 follows até 03/11/2026 e
~20 em 07/11. Com a frota fazendo ~875 follows por dia, o chat precisa de dezenas de posts novos por
dia, o que não fecha na mão.

## Como escolhe o post

O que importa é **curtida** e **foto de roupa**. Não tem filtro de seguidor (com 2 mil o teste de
07/11/2026 achou 0 em 82 curtidores: quase todos são privados ou pequenos). Só fica uma trava de
famoso: perfil com mais de 100 mil seguidores tem público do mundo todo, não a turma.

1. **Sementes:** os autores dos posts que já passaram pelo chat (`posts_feitos.json` do Auto Follow)
   e os perfis que o próprio Garimpo já mandou. Assim a busca vai saindo da turma atual pra turmas
   novas (bola de neve).
2. Abre o post mais novo da semente e lê **quem curtiu**. Vai curtidor por curtidor (a foto de perfil
   na lista não diz nada: pode ser rosto, desenho, qualquer coisa).
3. **Grade:** abre o perfil do curtidor (privado sai na hora), sobe a grade e manda **um** print com as
   miniaturas numeradas pro Gemini, que diz quais são foto de roupa ou look. Perfil sem nenhuma (o
   barbeiro, a página de meme) sai sem abrir post.
4. **Post:** abre só as de roupa, na ordem da grade. Vale **qualquer tipo de post**: foto, carrossel,
   reel e vídeo (reel aberto pela grade cai no feed normal, igual foto). Passa o post deste ano
   (fixado velho é pulado), com pelo menos `min_curtidas` curtidas (padrão 5) e cuja lista de curtidas tem pelo menos
   `min_novos` pessoas (padrão 15) que a frota ainda **não** seguiu (todos os follows estão no
   `agente.log`). É o que evita post que rende 3 follows.
5. Manda no grupo (`grupo`, padrão `vaitomanoquasenada`) pelo botão de enviar do post.
6. **Rodízio de perfis bons:** perfil que rendeu post e tem pelo menos 2 fotos de roupa na grade entra
   numa lista. O bot alterna perfil novo e perfil bom, volta nos bons pra pegar o post seguinte (nunca
   o mesmo post duas vezes, nunca dois seguidos do mesmo perfil, no máximo 2 do mesmo perfil por run).
   Perfil bom que falha 2 vezes seguidas sai do rodízio.

## Estado

`garimpo_estado.json`, na raiz do device:

- `perfis`: curtidor avaliado e descartado não é reaberto por 14 dias (com o motivo);
- `bons`: o rodízio (desde quando, último envio, quantos, falhas seguidas);
- `posts`: todo post enviado (`autor|data|início da legenda`), pra nunca repetir;
- `enviados`: último envio de cada perfil;
- `sementes`: semente explorada descansa 2 dias.

## Chave do Gemini

A triagem da grade lê a chave de `gemini_key` no `agente_config.json` (ou das variáveis
`GEMINI_API_KEY` / `GOOGLE_API_KEY`). É **uma** chamada por perfil público (~3 s). Sem chave o bot roda
com a triagem **desligada** e avisa no log; com chave e o Gemini fora do ar 3 vezes seguidas, ele para a
conta em vez de mandar foto sem conferir.

## Onde roda

No agente do `quase-nada-bots-device`, disparado pelo app dos Bots: seleciona as contas e roda, igual
ao Like + Repost. A conta precisa estar no grupo. Modo `padrão`: `meta` (posts por conta),
`min_seguidores` (desligado), `min_curtidas` e `min_novos`.

```python
from bots.garimpo import garimpo

r = garimpo.processar_garimpo(d, log, conta, params, parar)
# → {enviados, perfis, descartados, falhas, motivo, fotos}
```
