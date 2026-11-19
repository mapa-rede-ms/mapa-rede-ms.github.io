"""Extrator da rede oficial MSGÁS e gerador do HTML único com camada GPS."""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

try:
    import truststore

    # Injeta o repositório de certificados do sistema (útil no Windows corporativo)
    truststore.inject_into_ssl()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "templates" / "mapa_template.html"
CACHE_PATH = BASE_DIR / "dados" / "rede_msgas_cache.json"
OUTPUT_HTML_PATH = BASE_DIR / "dist" / "index.html"
SHOWMAPA_URL = "https://nossarede.msgas.com.br/showmapa"
PUBLIC_MAP_URL = "https://mapa-rede-ms.github.io/"
CIDADES_PERMITIDAS = ("Campo Grande", "Dourados", "Três Lagoas")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _extrair_viewstate(texto: str) -> str | None:
    """Extrai o token jakarta.faces.ViewState do HTML inicial ou de resposta AJAX XML."""
    match_xml = re.search(
        r"<update[^>]*ViewState[^>]*><!\[CDATA\[([^\]]+)\]\]>", texto
    )
    if match_xml:
        return match_xml.group(1)

    match_html = re.search(
        r'name="jakarta\.faces\.ViewState"[^>]*value="([^"]+)"', texto
    )
    if match_html:
        return match_html.group(1)

    return None


def _descobrir_metadados_jsf(html: str) -> dict[str, Any]:
    """Identifica dinamicamente form_id, remoteCommand e itens de cidades no HTML."""
    form_match = re.search(
        r'<form[^>]*id="([^"]+)"[^>]*action="/showmapa"', html
    )
    form_id = form_match.group(1) if form_match else "j_idt17"

    rc_match = re.search(
        r'getMapData\s*=\s*function\s*\(\)\s*\{return\s+pf\.ab\(\{s:"([^"]+)"[^}]*f:"([^"]+)"',
        html,
    )
    remote_cmd = rc_match.group(1) if rc_match else "getMapa"
    if rc_match and rc_match.group(2):
        form_id = rc_match.group(2)

    cidades = re.findall(
        r'<a\s+id="([^"]+)"[^>]*><span class="ui-menuitem-text">'
        r"Centralizar mapa em ([^<]+)</span></a>",
        html,
    )

    return {
        "form_id": form_id,
        "remote_cmd": remote_cmd,
        "cidades": cidades,
    }


def _parse_args_primefaces(xml_text: str) -> dict[str, Any]:
    """Lê o bloco JSON <extension type='args'> de uma resposta partial/ajax PrimeFaces."""
    root = ET.fromstring(xml_text)
    for ext in root.iter("extension"):
        if ext.attrib.get("type") == "args" and ext.text:
            return json.loads(ext.text)
    raise ValueError("Resposta XML do PrimeFaces não contém bloco 'extension[type=args]'.")


def _chamar_get_mapa(
    session: requests.Session,
    form_id: str,
    remote_cmd: str,
    viewstate: str,
) -> tuple[dict[str, Any], str]:
    """Executa o remoteCommand getMapa e devolve (args_dict, novo_viewstate)."""
    resp = session.post(
        SHOWMAPA_URL,
        data={
            "jakarta.faces.partial.ajax": "true",
            "jakarta.faces.source": remote_cmd,
            "jakarta.faces.partial.execute": remote_cmd,
            "jakarta.faces.partial.render": "mapa",
            remote_cmd: remote_cmd,
            form_id: form_id,
            "jakarta.faces.ViewState": viewstate,
        },
        headers={"Faces-Request": "partial/ajax"},
        timeout=45,
    )
    resp.raise_for_status()
    novo_vs = _extrair_viewstate(resp.text) or viewstate
    args = _parse_args_primefaces(resp.text)
    return args, novo_vs


def extrair_dados_oficiais(buscar_cidades: bool = True) -> dict[str, Any]:
    """Consulta o site oficial da MSGÁS e extrai o GeoJSON da rede e as cidades."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print(f"[1/3] Conectando em {SHOWMAPA_URL} ...")
    resp_get = session.get(SHOWMAPA_URL, timeout=20)
    resp_get.raise_for_status()

    viewstate = _extrair_viewstate(resp_get.text)
    if not viewstate:
        raise RuntimeError("Não foi possível localizar jakarta.faces.ViewState na página inicial.")

    meta = _descobrir_metadados_jsf(resp_get.text)
    form_id = meta["form_id"]
    remote_cmd = meta["remote_cmd"]

    print(f"[2/3] Baixando GeoJSON oficial da rede (form={form_id}, cmd={remote_cmd}) ...")
    args_inicial, viewstate = _chamar_get_mapa(session, form_id, remote_cmd, viewstate)
    geojson_obj = json.loads(args_inicial["data"])
    default_center = args_inicial.get("centerMap", "-20.459574,-54.603473")
    default_zoom = int(args_inicial.get("zoom", 15))

    qtd_trechos = len(geojson_obj.get("features", []))
    print(f"      -> {qtd_trechos} trechos extraídos com sucesso.")

    cidades_extraidas: list[dict[str, Any]] = []
    if buscar_cidades and meta["cidades"]:
        cidades_alvo = [
            (cid_id, nome)
            for cid_id, nome in meta["cidades"]
            if nome.strip() in CIDADES_PERMITIDAS
        ]
        total = len(cidades_alvo)
        print(f"[3/3] Coletando coordenadas oficiais de {total} municípios permitidos ...")
        for idx, (cid_id, nome) in enumerate(cidades_alvo, start=1):
            try:
                rc = session.post(
                    SHOWMAPA_URL,
                    data={
                        "jakarta.faces.partial.ajax": "true",
                        "jakarta.faces.source": cid_id,
                        "jakarta.faces.partial.execute": cid_id,
                        "jakarta.faces.partial.render": "mapa",
                        cid_id: cid_id,
                        form_id: form_id,
                        "jakarta.faces.ViewState": viewstate,
                    },
                    headers={"Faces-Request": "partial/ajax"},
                    timeout=20,
                )
                rc.raise_for_status()
                viewstate = _extrair_viewstate(rc.text) or viewstate

                args_cid, viewstate = _chamar_get_mapa(
                    session, form_id, remote_cmd, viewstate
                )
                cidades_extraidas.append(
                    {
                        "nome": nome.strip(),
                        "centerMap": args_cid.get("centerMap", default_center),
                        "zoom": int(args_cid.get("zoom", 15)),
                    }
                )
                print(f"      [{idx}/{total}] {nome.strip()} -> {args_cid.get('centerMap')}")
            except Exception as exc:
                print(f"      [AVISO] Falha ao consultar {nome}: {exc}")
    else:
        print("[3/3] Usando lista de cidades em cache ou padrão.")

    if not cidades_extraidas:
        cidades_extraidas = [
            {
                "nome": "Campo Grande",
                "centerMap": default_center,
                "zoom": default_zoom,
            }
        ]

    agora_br = datetime.now().strftime("%d/%m/%Y %H:%M")
    return {
        "sincronizado_em": agora_br,
        "sincronizado_iso": datetime.now().isoformat(timespec="seconds"),
        "fonte_url": SHOWMAPA_URL,
        "default_center": default_center,
        "default_zoom": default_zoom,
        "total_trechos": qtd_trechos,
        "cidades": cidades_extraidas,
        "geojson": geojson_obj,
    }


def _json_seguro_para_script(dados: Any) -> str:
    """Serializa JSON compacto escapando '</' para inserção segura dentro de tag <script>."""
    bruto = json.dumps(dados, ensure_ascii=False, separators=(",", ":"))
    return bruto.replace("</", "<\\/")


def salvar_cache(dados: dict[str, Any], caminho: Path = CACHE_PATH) -> None:
    """Persiste os dados extraídos em JSON local para histórico e fallback offline."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8") as arq:
        json.dump(dados, arq, ensure_ascii=False, separators=(",", ":"))


def carregar_cache(caminho: Path = CACHE_PATH) -> dict[str, Any]:
    """Lê os dados previamente salvos no cache local."""
    if not caminho.exists():
        raise FileNotFoundError(f"Cache local não encontrado em: {caminho}")
    with caminho.open("r", encoding="utf-8") as arq:
        return json.load(arq)


def filtrar_cidades_permitidas(cidades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mantém apenas os municípios com tubulação ativa na ordem definida."""
    mapa_por_nome = {
        str(c.get("nome", "")).strip(): c for c in cidades if isinstance(c, dict)
    }
    return [mapa_por_nome[nome] for nome in CIDADES_PERMITIDAS if nome in mapa_por_nome]


def gerar_html_unico(
    dados: dict[str, Any],
    template_path: Path = TEMPLATE_PATH,
    output_path: Path = OUTPUT_HTML_PATH,
) -> Path:
    """Injeta GeoJSON, cidades e metadados no template e grava o HTML único final."""
    if not template_path.exists():
        raise FileNotFoundError(f"Template HTML não encontrado em: {template_path}")

    template = template_path.read_text(encoding="utf-8")
    cidades_filtradas = filtrar_cidades_permitidas(dados.get("cidades", []))

    meta_payload = {
        "defaultCenter": dados.get("default_center", "-20.459574,-54.603473"),
        "defaultZoom": dados.get("default_zoom", 15),
        "totalTrechos": len(dados.get("geojson", {}).get("features", [])),
        "publicBaseUrl": PUBLIC_MAP_URL,
    }

    html_final = (
        template.replace("__REDE_GEOJSON__", _json_seguro_para_script(dados["geojson"]))
        .replace("__CIDADES_JSON__", _json_seguro_para_script(cidades_filtradas))
        .replace("__META_JSON__", _json_seguro_para_script(meta_payload))
        .replace("__DATA_SYNC__", str(dados.get("sincronizado_em", "")))
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_final, encoding="utf-8")
    return output_path


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada CLI para atualizar os dados oficiais e gerar dist/index.html."""
    parser = argparse.ArgumentParser(
        description="Extrai a rede oficial da MSGÁS e gera HTML único com camada GPS."
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Recompila dist/index.html utilizando apenas o cache local.",
    )
    parser.add_argument(
        "--no-cities",
        action="store_true",
        help="Pula a consulta individual das 3 cidades e mantém apenas o cache/padrão.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_HTML_PATH,
        help="Caminho de saída do HTML gerado (padrão: dist/index.html).",
    )
    parser.add_argument(
        "--no-cache-save",
        action="store_true",
        help="Não salva dados/rede_msgas_cache.json (ideal para execução em CI/GitHub Actions).",
    )
    args = parser.parse_args(argv)

    if args.offline:
        print("Modo --offline ativado: lendo cache local...")
        dados = carregar_cache()
    else:
        try:
            dados = extrair_dados_oficiais(buscar_cidades=not args.no_cities)
            if args.no_cities and CACHE_PATH.exists():
                cache_antigo = carregar_cache()
                if cache_antigo.get("cidades"):
                    dados["cidades"] = cache_antigo["cidades"]
            if not args.no_cache_save:
                salvar_cache(dados)
                print(f"Cache salvo em: {CACHE_PATH}")
        except Exception as exc:
            print(f"[ERRO] Falha ao consultar {SHOWMAPA_URL}: {exc}")
            if CACHE_PATH.exists():
                print("[FALLBACK] Usando último cache local válido para gerar o HTML...")
                dados = carregar_cache()
            else:
                return 1

    saida = gerar_html_unico(dados, output_path=args.output)
    tamanho_mb = saida.stat().st_size / (1024 * 1024)
    print("=" * 64)
    print(f"HTML único gerado com sucesso!")
    print(f"Arquivo : {saida}")
    print(f"Tamanho : {tamanho_mb:.2f} MB")
    print(f"Trechos : {len(dados.get('geojson', {}).get('features', []))}")
    print(f"Cidades : {len(filtrar_cidades_permitidas(dados.get('cidades', [])))}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
