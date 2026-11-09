# Changelog

## [1.1.0] — 2026-11-08

### Adicionado
- feat: o post escolhido também é curtido e republicado pela conta antes de ir pro grupo

### Corrigido
- fix: perfil privado escrito "Este perfil é privado" pula na hora (antes esperava ~40 s a grade)
- fix: rodízio volta a cada 4 perfis novos sem envio — não fica preso esperando um novo dar certo
- fix: "20,7 m" é 20,7 mil (o "m" minúsculo virava milhão e perfil comum caía como famoso)

### Documentação
- docs: README com o curtir e republicar

## [1.0.0] — 2026-11-07

### Adicionado
- feat: bot Garimpo no emulador — acha posts novos pro chat do Auto Follow: vai curtidor por curtidor da turma, olha a grade de cada um e o Gemini marca as fotos de roupa numa chamada só por perfil, confere curtidas e se a lista de curtidas ainda rende pra frota e manda no grupo
- feat: rodízio de perfis bons — perfil que rendeu post e tem várias fotos de roupa volta na lista, alternado com perfis novos, sem repetir post
- feat: bola de neve — o perfil enviado vira semente da próxima rodada
- feat: pula post de outro ano (fixado velho) e perfil famoso (100 mil+)
- feat: qualquer tipo de post (foto, carrossel, reel e vídeo)
- feat: conta com o direct bloqueado é pulada e o 1º envio é conferido no grupo ("Falha ao enviar")
- feat: estado em `garimpo_estado.json` (perfis avaliados, enviados, sementes) pra não repetir

### Documentação
- docs: README do bot
