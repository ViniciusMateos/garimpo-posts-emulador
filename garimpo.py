"""Garimpo (07/11/2026, pedido do Vinicius): acha posts NOVOS pro chat do auto-follow sozinho.

Por quê: a lista de curtidas do IG mostra no máximo ~100 pessoas e os posts do chat são todos da
mesma turma — até 03/11 cada post rendia ~95 follows, em 07/11 rendia ~20 (quem curte já foi
seguido). A frota gasta ~875 follows/dia = ~40 posts/dia; à mão não fecha.

O critério é o dele (o que importa é CURTIDA e FOTO DE ROUPA — sem filtro de seguidor):
  1. sementes = autores dos posts que já passaram pelo chat (posts_feitos.json) + os perfis que o
     próprio Garimpo já mandou (bola de neve: a rede vai saindo da turma de hoje);
  2. entra no perfil da semente, abre o post mais novo e lê QUEM CURTIU → candidatos;
  3. perfil público do candidato: olha a GRADE de uma vez (1 print numerado pro Gemini diz quais
     miniaturas são foto de roupa) — perfil sem roupa nenhuma (o barbeiro) sai sem abrir nada;
  4. abre só as de roupa: post deste ano, com >= `min_curtidas` curtidas e lista de curtidas com
     >= `min_novos` pessoas que a frota ainda NÃO seguiu (o agente.log tem todos os follows);
  5. manda no grupo (botão de enviar do post → grupo → Enviar);
  6. PERFIL BOM (rendeu post e tem várias fotos de roupa) entra no RODÍZIO: o bot alterna perfil
     novo com perfil bom e volta nos bons pra pegar os posts seguintes (nunca o mesmo post 2x).
Nunca segue, curte nem comenta — só olha e envia no grupo.

Estado (garimpo_estado.json, na raiz do device): perfis avaliados, perfis bons, posts enviados e
sementes já exploradas."""
import base64
import io
import json
import os
import random
import re
import time
import urllib.request

from bots.auto_follow import auto_follow as af
from bots.auto_follow.auto_follow import _attr, _bounds, _dump, _log, _sleep, rid
from bots.brecho_scraper import brecho_scraper as bs

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
ESTADO_PATH = os.path.join(BASE, "garimpo_estado.json")
AGENTE_LOG = os.path.join(BASE, "agente.log")
CONFIG_PATH = os.path.join(BASE, "agente_config.json")
DEBUG_DIR = os.path.join(BASE, "logs", "garimpo")

PADRAO = {
    "meta": 10,                    # posts enviados por conta nesta run
    "min_seguidores": 0,           # 0 = sem filtro (07/11: "o que importa é curtida e foto de roupa")
    "max_seguidores": 100000,      # trava de famoso (o rapper de 3 mi, 07/11): quem curte é o mundo todo
    "min_curtidas": 5,             # curtidas da foto escolhida
    "min_novos": 15,               # pessoas na lista de curtidas que a frota ainda não seguiu
    "grupo": "vaitomanoquasenada",
    "curtidores_por_semente": 40,  # quantos curtidores lê de cada semente
    "posts_por_perfil": 4,         # quantas fotos de roupa da grade abre atrás de uma que renda
    "teto_perfis": 150,            # perfis de candidato abertos por conta (~4/5 dos curtidores são privados)
    "sementes_por_run": 12,        # sementes exploradas por conta
    "revisitar_dias": 14,          # perfil avaliado (e descartado) não é reaberto antes disso
    "semente_dias": 2,             # semente explorada descansa esse tanto
    "bom_min_roupa": 2,            # fotos de roupa na grade pra virar perfil BOM (entra no rodízio)
    "bom_por_run": 2,              # posts de um mesmo perfil bom por run (não lotar o chat de 1 perfil)
    "bom_falhas": 2,               # perfil bom que falha isso seguido sai do rodízio
    "pausa": (2.0, 5.0),           # respiro entre perfis (atividade de gente)
}

_RE_FOLLOW = re.compile(r"^\[..:..:..\]   \+ @(\S+)  \(\d+/\d+\)")
_RE_NUM = re.compile(r"(\d[\d.,\s]*)\s*(milhões|milhão|mil|mi|k|m)?", re.I)
SEGUIDORES_KW = ("seguidores", "seguidor", "followers", "follower")
NAO_CANDIDATOS = ("quasenada", "brechoquasenada")
# privado escrito de outro jeito: "Este perfil é privado" (IG da segue35, 07/11) — o abrir_perfil do
# scraper só conhecia "Esta conta é privada" e ficava ~40 s esperando a grade de cada privado
_PRIVADO_EXTRA = ("este perfil é privado", "este perfil e privado", "this profile is private")
bs.PERFIL_RUIM_TXT = tuple(dict.fromkeys(tuple(bs.PERFIL_RUIM_TXT) + _PRIVADO_EXTRA))


class GarimpoParado(Exception):
    pass


class VisaoFora(Exception):
    """A chave existe mas o Gemini não respondeu 3 fotos seguidas (cota/fora do ar): para a conta em
    vez de mandar foto sem conferir."""


_VISAO = {"fotos": 0, "falhas_seguidas": 0}


# ─────────────────────────────── estado ───────────────────────────────
def _carregar():
    try:
        with open(ESTADO_PATH, "r", encoding="utf-8") as f:
            est = json.load(f)
    except Exception:
        est = {}
    for k in ("perfis", "enviados", "sementes", "bons", "posts"):
        est.setdefault(k, {})
    return est


def _salvar(est):
    try:
        tmp = ESTADO_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(est, f, ensure_ascii=False)
        os.replace(tmp, ESTADO_PATH)
    except Exception:
        pass


def _recente(ts, dias):
    return bool(ts) and (time.time() - float(ts)) < dias * 86400


def seguidos_frota():
    """Todo mundo que alguma conta da frota já seguiu (as linhas '+ @user (k/n)' do agente.log)."""
    out = set()
    try:
        with io.open(AGENTE_LOG, encoding="utf-8", errors="replace") as f:
            for linha in f:
                m = _RE_FOLLOW.match(linha)
                if m:
                    out.add(m.group(1).lower())
    except Exception:
        pass
    return out


def autores_do_chat():
    """{autor: ts} dos posts que já passaram pelo chat (posts_feitos.json do auto-follow)."""
    try:
        with open(af._FEITOS_PATH, "r", encoding="utf-8") as f:
            reg = json.load(f)
    except Exception:
        reg = {}
    out = {}
    for k, v in reg.items():
        a = k.partition("|")[0].strip().lower()
        if a and a != "?":
            out[a] = max(out.get(a, 0), float(v or 0))
    return out


def sementes(est, cfg):
    """Ordem: perfis que o Garimpo mandou (mais novos primeiro — a rede crescendo pra fora), depois
    os autores do chat (mais novos primeiro). Pula a que foi explorada há menos de `semente_dias`."""
    env = sorted(est["enviados"].items(), key=lambda kv: -float(kv[1].get("ts", 0)))
    chat = sorted(autores_do_chat().items(), key=lambda kv: -kv[1])
    vistos, out = set(), []
    for u, _ in env + chat:
        if u in vistos or _recente(est["sementes"].get(u), cfg["semente_dias"]):
            continue
        vistos.add(u)
        out.append(u)
    return out


def _cfg(params):
    c = dict(PADRAO)
    for k in PADRAO:
        v = (params or {}).get(k)
        if v in (None, ""):
            continue
        if isinstance(PADRAO[k], int):
            try:
                c[k] = max(0, int(v))
            except (TypeError, ValueError):
                pass
        elif isinstance(PADRAO[k], str):
            c[k] = str(v).strip() or PADRAO[k]
    return c


# ─────────────────────────────── visão ───────────────────────────────
MODELOS = ("gemini-flash-lite-latest", "gemini-flash-latest", "gemini-3.5-flash-lite", "gemini-3.5-flash")
PROMPT_ROUPA = (
    "Esta e a foto de um post do Instagram. Responda SO com JSON {\"roupa\": true|false}. "
    "roupa=true SO quando a roupa e o foco: uma pessoa mostrando o look (corpo inteiro ou meio corpo, foto "
    "de outfit, estilo, moda de rua, a pessoa ocupa boa parte da foto) ou uma peca de roupa, tenis ou "
    "acessorio em destaque. roupa=false para meme, print de texto ou de conversa, paisagem, mural, arte, "
    "palco ou show, comida, carro, animal, close so do rosto, foto escura ou borrada, foto antiga de "
    "familia, ou foto em grupo onde a roupa nao e o foco.")


def _chave_gemini():
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return (json.load(f).get("gemini_key") or "").strip()
    except Exception:
        return ""


def foto_de_roupa(jpeg, log=None):
    """True/False pelo modelo de visão; None se não deu pra perguntar (sem chave / todos falharam)."""
    chave = _chave_gemini()
    if not chave or not jpeg:
        return None
    corpo = json.dumps({
        "contents": [{"parts": [{"text": PROMPT_ROUPA},
                                {"inline_data": {"mime_type": "image/jpeg",
                                                 "data": base64.b64encode(jpeg).decode()}}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }).encode()
    for modelo in MODELOS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
        req = urllib.request.Request(url, data=corpo, headers={"Content-Type": "application/json",
                                                               "x-goog-api-key": chave})
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=25).read())
            txt = r["candidates"][0]["content"]["parts"][0]["text"]
            return bool(json.loads(txt).get("roupa"))
        except Exception as e:
            _log(log, f"  (visão {modelo} falhou: {str(e)[:80]})")
            continue
    return None


def _jpeg_pequeno(jpeg, lado=640):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(jpeg)).convert("RGB")
        im.thumbnail((lado, lado))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=80)
        return b.getvalue()
    except Exception:
        return jpeg


PROMPT_GRADE = (
    "Esta imagem e uma grade de miniaturas NUMERADAS de posts de um perfil do Instagram. Responda SO com "
    "JSON {\"roupa\": [numeros]} com os numeros das miniaturas em que a ROUPA e o foco: uma pessoa "
    "mostrando o look (corpo inteiro ou meio corpo, foto de outfit, estilo, moda de rua) ou uma peca de "
    "roupa, tenis ou acessorio em destaque. NAO inclua: meme, print, texto, paisagem, mural, arte, palco, "
    "comida, carro, animal, close so do rosto, corte de cabelo, biquini ou roupa de banho, foto escura, "
    "foto antiga de familia. "
    "Lista vazia se nenhuma.")


def _gemini_json(jpeg, prompt, log=None):
    """Pergunta ao Gemini com a imagem; devolve o JSON da resposta ou None."""
    chave = _chave_gemini()
    if not chave or not jpeg:
        return None
    corpo = json.dumps({
        "contents": [{"parts": [{"text": prompt},
                                {"inline_data": {"mime_type": "image/jpeg",
                                                 "data": base64.b64encode(jpeg).decode()}}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }).encode()
    for modelo in MODELOS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
        req = urllib.request.Request(url, data=corpo, headers={"Content-Type": "application/json",
                                                               "x-goog-api-key": chave})
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=30).read())
            return json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as e:
            _log(log, f"  (visão {modelo} falhou: {str(e)[:80]})")
    return None


def _subir_grade(d):
    """Rola o perfil pra grade ocupar a tela (o cabeçalho sobe) — cabem ~4 linhas de miniaturas."""
    try:
        W, H = d.window_size()
    except Exception:
        W, H = 900, 1600
    d.swipe(W // 2, int(H * 0.80), W // 2, int(H * 0.38), 0.35)
    _sleep(1.0, 1.5)


VIDEO_DESC = ("reel", "vídeo", "video", "clip")


def _celulas_com_desc(d):
    """{(linha, coluna): (bounds, desc)} das miniaturas visíveis."""
    out = {}
    for m in re.finditer(r"<node[^>]*/?>", _dump(d)):
        t = m.group(0)
        desc = _attr(t, "content-desc")
        mm = bs._RE_GRID_POS.search(desc) or bs._RE_GRID_POS_EN.search(desc)
        bb = _bounds(t)
        if mm and bb:
            out[(int(mm.group(1)), int(mm.group(2)))] = (bb, desc)
    return out


def _eh_video(desc):
    """Miniatura/mídia de reel ou vídeo solto ("Reel de X…", "Vídeo de X…"). Carrossel com vídeo dentro
    ("10 fotos e 3 vídeos de X") é post normal."""
    low = (desc or "").strip().lower()
    return low.startswith(VIDEO_DESC)


def post_eh_video(xml):
    """O post em foco é reel/vídeo (a mídia tem content-desc 'Reel…'/'Vídeo…' ou o viewer de clips)."""
    nos = []
    for m in re.finditer(r"<node[^>]*/?>", xml):
        t = m.group(0)
        bb = _bounds(t)
        if bb:
            nos.append((bb[1], _attr(t, "resource-id"), _attr(t, "content-desc")))
    cab = sorted(y for y, r, _ in nos if r.endswith(":id/row_feed_profile_header"))
    y0 = cab[0] if cab else 0
    y1 = cab[1] if len(cab) > 1 else 10 ** 6
    for y, r, desc in nos:
        if y0 <= y < y1 and (("clips" in r and "media" in r) or (_eh_video(desc) and "gostos" in desc.lower())):
            return True
    return False


def grade_roupa(d, log=None):
    """Miniaturas da grade VISÍVEL que são foto de roupa, pelo Gemini numa imagem só (montagem com
    números). Devolve (roupa, total): roupa = [(linha, coluna, bounds)] na ordem da grade (novo →
    antigo); total = miniaturas avaliadas. Sem chave: todas passam. None se não deu pra conferir."""
    from PIL import Image, ImageDraw
    try:
        W, H = d.window_size()
    except Exception:
        W, H = 900, 1600
    # só as inteiras (a de baixo cortada pela borda vem com altura menor). Reel/vídeo ENTRA (07/11:
    # "todo tipo de post") — aberto pela grade ele cai no feed normal, igual foto.
    cel = {k: bb for k, (bb, desc) in _celulas_com_desc(d).items()
           if (bb[3] - bb[1]) >= 0.85 * (bb[2] - bb[0]) and bb[3] <= H}
    if not cel:
        return [], 0
    ordem = sorted(cel)
    if not _chave_gemini():
        return [(l, c, cel[(l, c)]) for (l, c) in ordem], len(ordem)
    im = bs._print_tela(d)
    if im is None:
        return None
    iw, ih = im.size
    sx, sy = iw / float(W or iw), ih / float(H or ih)
    lado = 220
    cols = 3
    linhas = (len(ordem) + cols - 1) // cols
    mont = Image.new("RGB", (cols * lado, linhas * lado), (255, 255, 255))
    dr = ImageDraw.Draw(mont)
    for i, k in enumerate(ordem):
        x1, y1, x2, y2 = cel[k]
        mini = im.crop((int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))).convert("RGB")
        mini.thumbnail((lado - 6, lado - 6))
        ox, oy = (i % cols) * lado + 3, (i // cols) * lado + 3
        mont.paste(mini, (ox, oy))
        dr.rectangle((ox, oy, ox + 34, oy + 30), fill=(255, 230, 0))
        dr.text((ox + 8, oy + 6), str(i + 1), fill=(0, 0, 0))
    b = io.BytesIO()
    mont.save(b, "JPEG", quality=85)
    _VISAO["fotos"] += 1
    r = _gemini_json(b.getvalue(), PROMPT_GRADE, log)
    if r is None:
        _VISAO["falhas_seguidas"] += 1
        if _VISAO["falhas_seguidas"] >= 3:
            raise VisaoFora()
        return None
    _VISAO["falhas_seguidas"] = 0
    nums = []
    for n in (r.get("roupa") or []) if isinstance(r, dict) else []:
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(ordem) and n not in nums:
            nums.append(n)
    nums.sort()
    return [(ordem[n - 1][0], ordem[n - 1][1], cel[ordem[n - 1]]) for n in nums], len(ordem)


_RE_DATA = re.compile(r"\b(\d{1,2} de \w+( de \d{4})?|há \d+ \w+|\d+ (sem|d|h|min)\b)", re.I)


def chave_post(xml, autor):
    """Identidade do post em foco (pra nunca mandar o mesmo 2x): autor|data|início da legenda."""
    nos = []
    for m in re.finditer(r"<node[^>]*/?>", xml):
        t = m.group(0)
        bb = _bounds(t)
        if bb:
            nos.append((bb[1], _attr(t, "resource-id"), _attr(t, "text"), _attr(t, "content-desc")))
    cab = sorted((y, desc) for y, r, _t, desc in nos if r.endswith(":id/row_feed_profile_header"))
    y0 = cab[0][0] if cab else 0
    y1 = cab[1][0] if len(cab) > 1 else 10 ** 6
    data, leg = "", ""
    quem = {autor.lower()}
    if cab:   # "gabzeeni publicou um carousel 17 de outubro" / "tm.maria_ publicou um video 20 de março"
        m = _RE_DATA.search(cab[0][1] or "")
        data = m.group(1) if m else ""
        dono = (cab[0][1] or "").split(" publicou")[0].strip().lower()
        if dono and " " not in dono:
            quem.add(dono)
    for y, r, txt, _d in nos:
        if not (y0 <= y < y1) or not txt:
            continue
        if r.endswith((":id/row_feed_photo_profile_name", ":id/secondary_label")):
            continue          # nome do cabeçalho ("tm.maria_ e 2 outras pessoas"), não é a legenda
        if not data:
            m = _RE_DATA.search(txt)
            if m:
                data = m.group(1)
        if not leg:
            for q in quem:
                if txt.lower().startswith(q + " "):
                    leg = txt[len(q) + 1:].strip()[:50]
                    break
    return f"{autor.lower()}|{data.lower()}|{leg.lower()}"


# ─────────────────────────────── leitura de tela ───────────────────────────────
def _num_ig(txt):
    """'2.345' → 2345 · '12,3 mil' / '20,7 m' / '5 k' → milhar · '1,2 M' / '1,2 mi' → milhão · '980' → 980.
    O 'm' MINÚSCULO é milhar (IG destas contas: "27 mseguidores" = 27 mil); só o 'M' maiúsculo é milhão —
    por isso NÃO baixar a caixa do texto antes de chamar. None se não tem número."""
    m = _RE_NUM.search(txt or "")
    if not m:
        return None
    num, suf = m.group(1).strip(), (m.group(2) or "")
    if suf:
        try:
            v = float(re.sub(r"[.\s]", "", num).replace(",", "."))
        except ValueError:
            return None
        milhao = suf == "M" or suf.lower() in ("mi", "milhões", "milhão")
        return int(v * (1000000 if milhao else 1000))
    try:
        return int(re.sub(r"[.,\s]", "", num))
    except ValueError:
        return None


def ler_seguidores(xml):
    """Nº de seguidores do cabeçalho do perfil aberto. Procura o nó cujo content-desc/texto fala
    'seguidores' e lê o número dele (ou do vizinho de cima, quando número e rótulo são nós separados)."""
    nos = []
    for m in re.finditer(r"<node[^>]*/?>", xml):
        t = m.group(0)
        bb = _bounds(t)
        if bb:
            nos.append((_attr(t, "text"), _attr(t, "content-desc"), bb))
    for txt, desc, bb in nos:
        for s in (desc, txt):
            low = (s or "").lower()
            if any(k in low for k in SEGUIDORES_KW):
                corte = min(i for i in (low.find("segui"), low.find("follow"), len(low)) if i >= 0)
                n = _num_ig((s or "")[:corte])          # caixa ORIGINAL: 'm' = mil, 'M' = milhão
                if n is not None:
                    return n
                # rótulo sozinho → o número é o nó logo acima, na mesma coluna
                cx = (bb[0] + bb[2]) // 2
                acima = [(bb[1] - b[3], t2 or d2) for t2, d2, b in nos
                         if b[3] <= bb[1] + 4 and abs((b[0] + b[2]) // 2 - cx) < 60 and _num_ig(t2 or d2) is not None]
                if acima:
                    return _num_ig(min(acima)[1])
    return None


_RE_ANO = re.compile(r"\b\d{1,2} de \w+ de (20\d\d)\b")


def post_antigo(xml):
    """O post em foco é de um ANO anterior (o IG só escreve o ano na data quando não é deste ano) —
    pega o fixado velho e o post esquecido. Olha só entre o 1º cabeçalho de post e o 2º."""
    nos = []
    for m in re.finditer(r"<node[^>]*/?>", xml):
        t = m.group(0)
        bb = _bounds(t)
        if bb:
            nos.append((bb[1], _attr(t, "resource-id"), _attr(t, "text") or _attr(t, "content-desc")))
    cab = sorted(y for y, r, _ in nos if r.endswith(":id/row_feed_profile_header"))
    y0 = cab[0] if cab else 0
    y1 = cab[1] if len(cab) > 1 else 10 ** 6
    for y, _r, txt in nos:
        if y0 <= y < y1:
            m = _RE_ANO.search(txt or "")
            if m and int(m.group(1)) < time.localtime().tm_year:
                return True
    return False


def perfil_privado(xml):
    low = xml.lower()
    return any(k in low for k in bs.PERFIL_RUIM_TXT)


def ler_curtidas(xml):
    """(n_curtidas, bounds_do_contador) do post em foco no feed de detalhe. No IG 451 o número fica
    ao lado do coração (botão só com dígitos à direita do row_feed_button_like); no layout antigo é
    o like_count / 'N gostos'."""
    nos = []
    for m in re.finditer(r"<node[^>]*/?>", xml):
        t = m.group(0)
        bb = _bounds(t)
        if bb:
            nos.append({"rid": _attr(t, "resource-id"), "txt": _attr(t, "text"),
                        "desc": _attr(t, "content-desc"), "bb": bb})
    likes = [n for n in nos if n["rid"].endswith(":id/row_feed_button_like")]
    if likes:
        lk = min(likes, key=lambda n: n["bb"][1])
        lado = [n for n in nos if n["bb"][0] >= lk["bb"][2] - 6 and abs(n["bb"][1] - lk["bb"][1]) < 30
                and n["bb"][0] - lk["bb"][2] < 40 and _num_ig(n["txt"] or n["desc"]) is not None
                and not (n["desc"] or "").lower().startswith(("coment", "enviar", "republic"))]
        if lado:
            n = min(lado, key=lambda n: n["bb"][0])
            return _num_ig(n["txt"] or n["desc"]), n["bb"]
    for n in nos:
        bruto = n["txt"] or n["desc"] or ""
        s = bruto.lower()
        if n["rid"].endswith(":id/" + af.LIKE_COUNT) or re.search(r"\d\s*(gostos|curtidas|likes)\b", s):
            return _num_ig(bruto), n["bb"]
    return None, None


def _tocar_bb(d, bb):
    d.click((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2)


def ler_curtidores(d, maximo=120, log=None, parar=None):
    """Na folha de curtidas ABERTA: lê os @ rolando até o fim (ou `maximo`). Devolve a lista (ordem
    da folha), sem tocar em nada."""
    vistos, ordem = set(), []
    prev, parado = None, 0
    for _ in range(40):
        if parar and parar():
            raise GarimpoParado()
        xml = _dump(d)
        if not af._na_folha(xml):
            break
        nomes, _b = af._ler_folha(xml, 0, 10 ** 5)
        for n in nomes:
            k = n.strip().lstrip("@").lower()
            if k and k not in vistos:
                vistos.add(k)
                ordem.append(k)
        if len(ordem) >= maximo:
            break
        atual = tuple(nomes)
        parado = parado + 1 if atual == prev else 0
        if parado >= 2:
            break
        prev = atual
        af._scroll_folha(d)
        _sleep(1.0, 1.6)
    return ordem


def abrir_curtidores(d, bb_contador):
    """Toca no contador de curtidas e espera a folha 'Gostos' carregar a lista. True se abriu."""
    _tocar_bb(d, bb_contador)
    _sleep(1.8, 2.6)
    for _ in range(8):
        xml = _dump(d)
        if af._na_folha(xml) and ("/" + af.ROW_NAME) in xml:
            return True
        _sleep(0.8, 1.2)
    return False


def _fechar_folha(d):
    for _ in range(3):
        if not af._na_folha(_dump(d)):
            return
        d.press("back")
        _sleep(0.8, 1.2)


# ─────────────────────────────── envio no grupo ───────────────────────────────
# Folha de enviar (IG 451, medido 07/11): grade de conversas `direct_share_sheet_grid_view_pog` com
# content-desc "<nome> Conversa não selecionada" → toque vira "Conversa selecionada" e aparece o
# `direct_send_button_multi_select` ("Enviar"). Busca = `search_edit_text`.
POG = "direct_share_sheet_grid_view_pog"
BOTAO_ENVIAR = "direct_send_button_multi_select"


def _pog_do_grupo(xml, grupo):
    """(bounds, selecionado) da conversa `grupo` na folha de enviar, ou (None, False)."""
    g = grupo.lower()
    for m in re.finditer(r"<node[^>]*/?>", xml):
        t = m.group(0)
        desc = _attr(t, "content-desc").strip()
        low = desc.lower()
        if low.startswith(g) and "conversa" in low:
            return _bounds(t), ("não selecionada" not in low and "nao selecionada" not in low)
    return None, False


def _fechar_folha_envio(d):
    for _ in range(3):
        if not d(resourceId=rid("search_edit_text")).exists and not d(resourceId=rid(POG)).exists:
            return
        d.press("back")
        _sleep(0.7, 1.1)


def enviar_no_grupo(d, grupo, log=None):
    """Post em foco → botão de enviar → seleciona o grupo → Enviar. 'ok' | 'sem_grupo' | 'falha'.
    Confere que o grupo ficou "selecionada" antes do Enviar e que a folha fechou depois."""
    sh = bs._bounds_share(d)
    if not sh:
        return "falha"
    _tocar_bb(d, sh)
    bb, sel = None, False
    for _ in range(8):
        _sleep(0.7, 1.1)
        bb, sel = _pog_do_grupo(_dump(d), grupo)
        if bb or d(resourceId=rid("search_edit_text")).exists:
            break
    if not bb:
        busca = d(resourceId=rid("search_edit_text"))
        if not busca.exists:
            _log(log, "  a folha de enviar não abriu")
            _fechar_folha_envio(d)
            return "falha"
        busca.click()
        _sleep(0.5, 0.8)
        try:
            busca.set_text(grupo)
        except Exception:
            d.send_keys(grupo)
        for _ in range(6):
            _sleep(1.0, 1.4)
            bb, sel = _pog_do_grupo(_dump(d), grupo)
            if bb:
                break
        if not bb:   # resultado da busca em lista: qualquer nó que comece com o nome do grupo
            for m in re.finditer(r"<node[^>]*/?>", _dump(d)):
                t = m.group(0)
                tx = (_attr(t, "text") or _attr(t, "content-desc")).strip().lower()
                if tx.startswith(grupo.lower()) and "edit_text" not in _attr(t, "resource-id"):
                    bb = _bounds(t)
                    break
    if not bb:
        _log(log, f"  não achei o grupo '{grupo}' na folha de enviar — a conta está no grupo?")
        _fechar_folha_envio(d)
        return "sem_grupo"
    if not sel:
        _tocar_bb(d, bb)
        _sleep(1.0, 1.5)
    xml = _dump(d)
    _bb2, sel = _pog_do_grupo(xml, grupo)
    env = d(resourceId=rid(BOTAO_ENVIAR))
    if not env.exists:
        _log(log, "  o botão Enviar não apareceu depois de marcar o grupo — fechando sem enviar")
        _fechar_folha_envio(d)
        return "falha"
    if _bb2 and not sel:
        _log(log, "  o grupo não ficou marcado — fechando sem enviar")
        _fechar_folha_envio(d)
        return "falha"
    env.click()
    for _ in range(8):
        _sleep(0.8, 1.2)
        if not d(resourceId=rid(BOTAO_ENVIAR)).exists:
            _fechar_folha_envio(d)
            return "ok"
    _log(log, "  apertei Enviar mas a folha não fechou — conferir no grupo")
    _fechar_folha_envio(d)
    return "falha"


FALHA_ENVIO = ("falha ao enviar", "não foi possível enviar", "nao foi possivel enviar", "failed to send")


def _ir_ao_grupo(d, grupo, log=None):
    """Abre o grupo e vai pro fim (mais recente). True se abriu."""
    try:
        af.garantir_perfil(d, None)
        if not af.abrir_thread(d, grupo, None):
            return False
        _sleep(1.0, 1.5)
        af._ir_ao_fundo(d, None)
        _sleep(1.0, 1.5)
        return True
    except Exception:
        return False


def pode_enviar(d, grupo, log=None):
    """A conta consegue mandar mensagem no grupo? 'ok' | 'restrita' | 'sem_grupo'. Conta com o direct
    bloqueado ("Não podes enviar mensagens neste momento") abre o grupo sem caixa de mensagem — o
    envio pela folha FECHA normal mas o card fica com "Falha ao enviar" (segue31, 07/11)."""
    if not _ir_ao_grupo(d, grupo, log):
        return "sem_grupo"
    r = "restrita" if af.conta_restrita(d) else "ok"
    try:
        af.garantir_perfil(d, None)
    except Exception:
        pass
    return r


def envio_chegou(d, grupo, autor, log=None):
    """Confere no grupo o card mais recente do `autor`: True = chegou; False = 'Falha ao enviar';
    None = não deu pra conferir (não achou o card)."""
    if not _ir_ao_grupo(d, grupo, log):
        return None
    xml = _dump(d)
    cards, falhas = [], []
    for m in re.finditer(r"<node[^>]*/?>", xml):
        t = m.group(0)
        bb = _bounds(t)
        if not bb:
            continue
        txt = (_attr(t, "text") or "").strip().lower()
        desc = (_attr(t, "content-desc") or "").strip().lower()
        if _attr(t, "resource-id").endswith((":id/title", ":id/title_text")) and txt == autor.lower():
            cards.append(bb)
        if any(k in desc for k in FALHA_ENVIO):
            falhas.append(bb)
    try:
        af.garantir_perfil(d, None)
    except Exception:
        pass
    if not cards:
        return None
    topo = max(cards, key=lambda b: b[1])          # o card mais de baixo = o mais recente
    return not any(f[1] > topo[1] for f in falhas)


# ─────────────────────────────── perfis ───────────────────────────────
def _parar(parar):
    if parar and parar():
        raise GarimpoParado()


def _bloqueio(d):
    try:
        if af.conta_suspensa(d):
            return "suspensa"
        if af.desafio_humano(d):
            return "desafio_humano"
        b = af.tela_bloqueio(d)
        if b:
            return f"bloqueio:{b}"
    except Exception:
        pass
    return ""


def abrir_perfil(d, user, log=None):
    """Abre o perfil por link. 'ok' | 'privado' | 'indisponivel' | 'falha'."""
    try:
        bs.abrir_perfil(d, user, log=None)
        return "ok"
    except bs.ScraperAbort as e:
        s_ = str(e).lower()
        if "privado" in s_:
            return "privado"
        if "indispon" in s_:
            return "indisponivel"
        return "falha"
    except Exception:
        return "falha"


def curtidores_da_semente(d, semente, cfg, log, parar):
    """Abre o post mais novo da semente e lê quem curtiu. [] se não deu."""
    if abrir_perfil(d, semente, log) != "ok":
        return []
    _parar(parar)
    try:
        bs.entrar_no_feed(d, None)
    except Exception:
        return []
    _sleep(1.0, 1.6)
    n, bb = ler_curtidas(_dump(d))
    if not bb or not n:
        return []
    if not abrir_curtidores(d, bb):
        return []
    nomes = ler_curtidores(d, cfg["curtidores_por_semente"], log, parar)
    _fechar_folha(d)
    return nomes


def avaliar_perfil(d, user, cfg, frota, est, log, parar):
    """Abre o perfil, faz a triagem pela grade e abre as fotos de roupa atrás de UMA que renda (deste
    ano, curtidas, gente nova na lista, nunca enviada). Devolve (resultado, info); 'enviar' deixa o
    post ABERTO na tela pro envio e info["chave"] = identidade do post."""
    info = {}
    r = abrir_perfil(d, user, log)
    if r != "ok":
        return r, info
    _parar(parar)
    seg = ler_seguidores(_dump(d))
    info["seguidores"] = seg
    if seg is not None:
        if cfg["min_seguidores"] and seg < cfg["min_seguidores"]:
            return "poucos_seguidores", info
        if cfg["max_seguidores"] and seg > cfg["max_seguidores"]:
            return "famoso_demais", info
    _subir_grade(d)
    g = grade_roupa(d, log)
    if g is None:
        return "visao_falhou", info
    roupa, total = g
    info["roupa"] = f"{len(roupa)}/{total}"
    info["n_roupa"] = len(roupa)
    if not roupa:
        return "sem_roupa_na_grade", info
    tentou = 0
    for (lin, col, bb) in roupa:
        _parar(parar)
        if tentou >= max(1, cfg["posts_por_perfil"]):
            break
        if not bs._no_grid(d):
            bs._voltar_ao_grid(d, None)
        # a grade pode ter mexido: relê a célula pela posição
        atual = bs._celulas_visiveis(d).get((lin, col)) or bb
        _tocar_bb(d, atual)
        ok = False
        for _ in range(8):
            _sleep(0.8, 1.2)
            if bs._no_post(d):
                ok = True
                break
        if not ok:          # reel ou viewer diferente: pula
            bs._voltar_ao_grid(d, None)
            continue
        tentou += 1
        _sleep(0.6, 1.0)
        xml = _dump(d)
        chave = chave_post(xml, user)
        if chave in est["posts"]:
            info["repetido"] = info.get("repetido", 0) + 1
            bs._voltar_ao_grid(d, None)
            continue
        if post_antigo(xml):
            info["antigo"] = info.get("antigo", 0) + 1
            bs._voltar_ao_grid(d, None)
            continue
        n, bbc = ler_curtidas(xml)
        info["curtidas"] = n
        if not n or n < cfg["min_curtidas"] or not bbc:
            bs._voltar_ao_grid(d, None)
            continue
        if not abrir_curtidores(d, bbc):
            bs._voltar_ao_grid(d, None)
            continue
        nomes = ler_curtidores(d, 120, log, parar)
        _fechar_folha(d)
        novos = [u for u in nomes if u not in frota and not u.startswith(NAO_CANDIDATOS)]
        info["novos"] = len(novos)
        if len(novos) < cfg["min_novos"]:
            bs._voltar_ao_grid(d, None)
            continue
        if not bs._no_post(d):
            return "perdi_o_post", info
        info["chave"] = chave
        autor_card = re.search(r'resource-id="[^"]*row_feed_photo_profile_name"[^>]*text="([^"]+)"', xml) or \
            re.search(r'text="([^"]+)"[^>]*resource-id="[^"]*row_feed_photo_profile_name"', xml)
        info["autor_card"] = (autor_card.group(1).strip().split(" ")[0] if autor_card else "") or user
        return "enviar", info
    return "sem_foto_que_renda", info


# ─────────────────────────────── por conta ───────────────────────────────
def _candidatos(d, est, cfg, chat, log, parar, res):
    """Gerador de candidatos novos: semente → quem curtiu → filtra os já vistos/perfis bons."""
    for n_sem, semente in enumerate(sementes(est, cfg)):
        _parar(parar)
        if n_sem >= cfg["sementes_por_run"]:
            return
        _log(log, f"semente @{semente}: lendo quem curtiu o post mais novo…")
        nomes = curtidores_da_semente(d, semente, cfg, log, parar)
        est["sementes"][semente] = time.time()
        _salvar(est)
        b = _bloqueio(d)
        if b:
            res["motivo"] = b
            return
        cands = [u for u in nomes
                 if not u.startswith(NAO_CANDIDATOS)
                 and u not in chat and u not in est["bons"]
                 and not _recente(est["perfis"].get(u, {}).get("ts"), cfg["revisitar_dias"])]
        _log(log, f"  {len(nomes)} curtidor(es), {len(cands)} pra avaliar")
        for u in cands:
            yield u


def _proximo_bom(est, cfg, usados, ultimo):
    """Perfil bom pra esta vez: o que mandou há mais tempo, que ainda não deu `bom_por_run` nesta run
    e que não foi o último enviado (nunca 2 seguidos do mesmo perfil)."""
    cands = [(v.get("ult", 0), u) for u, v in est["bons"].items()
             if usados.get(u, 0) < cfg["bom_por_run"] and u != ultimo]
    return min(cands)[1] if cands else None


def processar_garimpo(d, log, conta, params=None, parar=None, rotulo=""):
    """Garimpa até `meta` posts e manda no grupo, alternando perfil NOVO e perfil BOM (rodízio).
    Devolve {enviados, perfis, descartados, falhas, motivo, fotos}."""
    cfg = _cfg(params)
    est = _carregar()
    frota = seguidos_frota()
    chat = autores_do_chat()
    rot = f"{rotulo} " if rotulo else ""
    res = {"enviados": 0, "perfis": 0, "descartados": 0, "falhas": 0, "motivo": "fim"}
    _VISAO.update(fotos=0, falhas_seguidas=0)
    meta = cfg["meta"] or cfg["teto_perfis"]   # meta desligada no app (0) = vai até o teto de perfis
    if not _chave_gemini():
        _log(log, "(sem a chave do Gemini no agente_config.json: a triagem de foto de roupa fica DESLIGADA)")
    _log(log, f"garimpo: {len(frota)} pessoas já seguidas pela frota, {len(chat)} autores no chat, "
              f"{len(est['bons'])} perfil(is) bom(ns) no rodízio")
    _log(log, f"[progress] 0 {meta} {rot}iniciando")
    pe = pode_enviar(d, cfg["grupo"], log)
    if pe != "ok":
        _log(log, f"!! @{conta}: " + ("o direct está BLOQUEADO (\"Não podes enviar mensagens\") — não dá pra "
                                   "mandar no grupo, pulo a conta" if pe == "restrita" else
                                   f"não achei o grupo '{cfg['grupo']}' — a conta está no grupo?"))
        res["motivo"] = "dm_restrita" if pe == "restrita" else "sem_grupo"
        _log(log, f"[progress] 0 {meta} {rot}concluído")
        return res
    conferido = False
    novos = _candidatos(d, est, cfg, chat, log, parar, res)
    usados, ultimo = {}, None
    vez_do_bom = bool(est["bons"])     # começa pelo rodízio quando já tem perfil bom
    acabou_novo = False

    def _enviar(u, info, de_bom):
        nonlocal ultimo
        e = enviar_no_grupo(d, cfg["grupo"], log)
        if e == "sem_grupo":
            res["motivo"] = "sem_grupo"
            return "parar"
        if e != "ok":
            res["falhas"] += 1
            _log(log, f"  ! @{u}: não consegui enviar no grupo")
            return "falha"
        nonlocal conferido
        if not conferido:
            chegou = envio_chegou(d, cfg["grupo"], info.get("autor_card") or u, log)
            if chegou is False:
                _log(log, f"!! o post de @{u} ficou com \"Falha ao enviar\" no grupo — a conta não consegue "
                          f"mandar mensagem agora; parando a conta")
                res["motivo"] = "dm_restrita"
                return "parar"
            conferido = chegou is True
            if chegou is None:
                _log(log, "  (não achei o card no grupo pra conferir — confiro no próximo envio)")
        res["enviados"] += 1
        agora = time.time()
        est["posts"][info["chave"]] = agora
        est["enviados"][u] = dict(info, ts=agora, conta=conta)
        usados[u] = usados.get(u, 0) + 1
        ultimo = u
        bom = est["bons"].get(u)
        if bom is not None:
            bom.update(ult=agora, n=bom.get("n", 0) + 1, falhas=0)
        elif info.get("n_roupa", 0) >= cfg["bom_min_roupa"]:
            est["bons"][u] = {"desde": agora, "ult": agora, "n": 1, "falhas": 0}
            _log(log, f"  * @{u} entrou no rodízio de perfis bons ({info.get('roupa')} fotos de roupa na grade)")
        _salvar(est)
        _log(log, f"  + @{u}: enviado no grupo{' (rodízio)' if de_bom else ''} — {info.get('curtidas')} "
                  f"curtidas, {info.get('novos')} novos na lista, roupa {info.get('roupa')}")
        _log(log, f"[progress] {res['enviados']} {meta} {rot}@{u}")
        return "ok"

    try:
        while res["enviados"] < meta:
            _parar(parar)
            if res["perfis"] >= cfg["teto_perfis"]:
                res["motivo"] = "teto"
                break
            u, de_bom = None, False
            if vez_do_bom or acabou_novo:
                u = _proximo_bom(est, cfg, usados, ultimo)
                de_bom = u is not None
            if u is None:
                if acabou_novo:
                    res["motivo"] = "sem_candidatos"
                    break
                try:
                    u = next(novos)
                except StopIteration:
                    acabou_novo = True
                    if res["motivo"].startswith(("bloqueio", "suspensa", "desafio")):
                        return res
                    continue
            res["perfis"] += 1
            r, info = avaliar_perfil(d, u, cfg, frota, est, log, parar)
            b = _bloqueio(d)
            if b:
                res["motivo"] = b
                return res
            if r == "enviar":
                e = _enviar(u, info, de_bom)
                if e == "parar":
                    return res
                if e == "ok":
                    vez_do_bom = not de_bom   # alterna: depois de um novo, um bom; depois de um bom, um novo
                _sleep(*cfg["pausa"])
                continue
            res["descartados"] += 1
            det = ", ".join(f"{k} {v}" for k, v in info.items()
                            if v is not None and k not in ("chave", "n_roupa", "autor_card"))
            _log(log, f"  - @{u}: {r}" + (" (rodízio)" if de_bom else "") + (f" ({det})" if det else ""))
            if de_bom:
                bom = est["bons"].get(u, {})
                bom["falhas"] = bom.get("falhas", 0) + 1
                usados[u] = cfg["bom_por_run"]          # não insiste nele nesta run
                if bom["falhas"] >= cfg["bom_falhas"]:
                    est["bons"].pop(u, None)
                    _log(log, f"  * @{u} saiu do rodízio ({bom['falhas']} vezes sem post que renda)")
                vez_do_bom = False
            else:
                est["perfis"][u] = dict({k: v for k, v in info.items() if k != "chave"}, ts=time.time(), r=r)
            _salvar(est)
            _sleep(*cfg["pausa"])
        if res["enviados"] >= meta:
            res["motivo"] = "meta"
    except GarimpoParado:
        res["motivo"] = "parado"
    except VisaoFora:
        _log(log, "!! o Gemini não respondeu 3 vezes seguidas (cota do dia ou fora do ar) — parando a conta "
                  "pra não mandar foto sem conferir")
        res["motivo"] = "visao_fora"
    finally:
        try:
            for _ in range(3):
                d.press("back")
                _sleep(0.4, 0.7)
            af.garantir_perfil(d, log)
        except Exception:
            pass
    res["fotos"] = _VISAO["fotos"]
    _log(log, f"perfis olhados no Gemini: {_VISAO['fotos']}")
    _log(log, f"[progress] {res['enviados']} {meta} {rot}concluído")
    return res
