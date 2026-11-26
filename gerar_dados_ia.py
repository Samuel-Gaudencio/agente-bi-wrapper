#!/usr/bin/env python3
"""Gera o dados_ia.json que alimenta o Assistente de Análise de BI.

Uso:
    python gerar_dados_ia.py                 # lê a planilha do Google Sheets
    python gerar_dados_ia.py --csv base.csv  # lê um CSV local (teste offline)

Principais garantias:
  * Os valores são lidos SEMPRE como texto (get_all_values). O get_all_records()
    do gspread converte "89,90" em 8990 e "1.031,80" em 1.0318, o que inflava
    venda/custo e gerava margens absurdas.
  * O parser entende formato brasileiro (1.031,80) e americano (1,031.80).
  * Antes de publicar, confere VENDA TOTAL = QTD x VENDA UNIT (e o mesmo para
    custo). Se algo estiver fora do esperado o script FALHA (o GitHub Actions
    fica vermelho) em vez de publicar números errados.
"""
import argparse
import json
import os
import re
import sys
import time

import numpy as np
import pandas as pd

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1Gm8w8-hMVUp4zYV-W4fQ7J_T2alPf4zsR1DynwLRKNM")
ABA_DADOS = os.environ.get("ABA_DADOS", "Dados")
SAIDA = os.environ.get("SAIDA", "dados_ia.json")
# "mdy" (1/31/2024), "dmy" (31/01/2024) ou vazio para detectar automaticamente
FORMATO_DATA = os.environ.get("FORMATO_DATA", "").lower()

COLUNAS_TEXTO = ["ID PEDIDO", "DATA", "PRODUTO", "CATEGORIA", "CLIENTE", "REGIÃO"]
COLUNAS_NUM = ["QTD", "VENDA UNIT", "VENDA TOTAL", "CUSTO UNIT", "CUSTO TOTAL"]
COLUNAS_OBRIGATORIAS = COLUNAS_TEXTO + COLUNAS_NUM

# Tolerâncias de qualidade
MAX_LINHAS_DESCARTADAS_PCT = 0.5   # acima disso o script falha
MAX_INCONSISTENTES_PCT = 0.5       # idem, para venda/custo que não fecham


class ErroDeDados(Exception):
    """Problema de dados que deve interromper a publicação."""


# --------------------------------------------------------------------------
# Leitura
# --------------------------------------------------------------------------
def ler_planilha() -> pd.DataFrame:
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    sa_json = os.environ.get("GCP_SA_KEY")
    if sa_json:
        creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=scopes)
    elif os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    else:
        raise ErroDeDados(
            "Credenciais não encontradas: defina o secret GCP_SA_KEY (GitHub Actions) "
            "ou coloque credentials.json na pasta."
        )

    client = gspread.authorize(creds)
    ultimo_erro = None
    for tentativa in range(1, 4):
        try:
            aba = client.open_by_key(SPREADSHEET_ID).worksheet(ABA_DADOS)
            # get_all_values devolve TUDO como texto, exatamente como aparece na planilha
            return valores_para_df(aba.get_all_values())
        except gspread.exceptions.APIError as e:  # limite de cota / instabilidade
            ultimo_erro = e
            time.sleep(2 ** tentativa)
    raise ErroDeDados(f"Falha ao ler a planilha após 3 tentativas: {ultimo_erro}")


def valores_para_df(valores) -> pd.DataFrame:
    if not valores or len(valores) < 2:
        raise ErroDeDados("A aba está vazia (sem cabeçalho ou sem linhas).")
    cab = [str(h).strip() for h in valores[0]]
    linhas = []
    for linha in valores[1:]:
        linha = [("" if c is None else str(c)).strip() for c in linha]
        linha = (linha + [""] * len(cab))[: len(cab)]
        linhas.append(linha)
    df = pd.DataFrame(linhas, columns=cab)
    return df.loc[:, [c for c in df.columns if c]]  # remove colunas sem nome


def ler_csv(caminho: str) -> pd.DataFrame:
    df = pd.read_csv(caminho, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    return df.apply(lambda c: c.str.strip())


# --------------------------------------------------------------------------
# Conversões
# --------------------------------------------------------------------------
def parse_numero(val) -> float:
    """Converte "R$ 1.031,80", "1,031.80", "89,90", 1031.8... em float. NaN se inválido."""
    if isinstance(val, (int, float, np.integer, np.floating)):
        return float(val)
    s = re.sub(r"[^\d,.\-]", "", str(val))  # tira R$, espaços (inclui NBSP), %...
    if s in ("", "-"):
        return np.nan
    if "," in s and "." in s:
        # o ÚLTIMO separador é o decimal
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        mil = "." if dec == "," else ","
        s = s.replace(mil, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        partes = s.split(sep)
        # um único separador seguido de 1-2 dígitos é decimal; 3 dígitos = milhar
        if len(partes) == 2 and len(partes[1]) != 3:
            s = partes[0] + "." + partes[1]
        else:
            s = s.replace(sep, "")
    try:
        return float(s)
    except ValueError:
        return np.nan


def parse_datas(serie: pd.Series) -> pd.Series:
    s = serie.astype(str).str.strip()
    partes = s.str.extract(r"^(\d{1,2})/(\d{1,2})/(\d{4})")
    casou = partes[0].notna().mean() >= 0.95
    if not casou:  # tenta ISO (2024-01-31) e afins
        return pd.to_datetime(s, errors="coerce")

    a, b = pd.to_numeric(partes[0]), pd.to_numeric(partes[1])
    if FORMATO_DATA in ("mdy", "dmy"):
        dayfirst = FORMATO_DATA == "dmy"
    elif (a > 12).any() and (b > 12).any():
        raise ErroDeDados("A coluna DATA mistura formatos (dia/mês e mês/dia). Padronize a planilha.")
    elif (a > 12).any():
        dayfirst = True
    elif (b > 12).any():
        dayfirst = False
    else:
        dayfirst = False  # padrão desta base: mês/dia/ano (ex.: 1/31/2024)
    fmt = "%d/%m/%Y" if dayfirst else "%m/%d/%Y"
    return pd.to_datetime(s, format=fmt, errors="coerce")


def brl(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------------------
# Preparação e validação
# --------------------------------------------------------------------------
def preparar(df: pd.DataFrame):
    faltando = [c for c in COLUNAS_OBRIGATORIAS if c not in df.columns]
    if faltando:
        raise ErroDeDados(f"Colunas ausentes na aba '{ABA_DADOS}': {faltando}. Encontradas: {list(df.columns)}")

    df = df.copy()
    df = df[df["ID PEDIDO"].astype(str).str.strip() != ""].reset_index(drop=True)
    lidas = len(df)
    if lidas == 0:
        raise ErroDeDados("Nenhuma linha com ID PEDIDO preenchido.")

    for col in COLUNAS_NUM:
        df[col] = df[col].map(parse_numero)
    df["DT"] = parse_datas(df["DATA"])

    invalido = df[COLUNAS_NUM + ["DT"]].isna().any(axis=1) | (df["PRODUTO"] == "")
    n_inv = int(invalido.sum())
    if n_inv:
        exemplos = df.loc[invalido, ["ID PEDIDO", "DATA", "QTD", "VENDA TOTAL", "CUSTO TOTAL"]].head(5)
        if n_inv / lidas * 100 > MAX_LINHAS_DESCARTADAS_PCT:
            raise ErroDeDados(
                f"{n_inv} de {lidas} linhas com data/número ilegível (>{MAX_LINHAS_DESCARTADAS_PCT}%). "
                f"Exemplos:\n{exemplos.to_string(index=False)}"
            )
        print(f"AVISO: {n_inv} linhas descartadas por data/número ilegível:\n{exemplos.to_string(index=False)}")
        df = df[~invalido].reset_index(drop=True)

    # --- Conferência de consistência (pega leitura errada de R$) ---
    tol_v = 0.01 * df["QTD"] + 0.02
    ruim_v = (df["VENDA TOTAL"] - df["QTD"] * df["VENDA UNIT"]).abs() > tol_v
    ruim_c = (df["CUSTO TOTAL"] - df["QTD"] * df["CUSTO UNIT"]).abs() > tol_v
    for nome, ruim in (("VENDA", ruim_v), ("CUSTO", ruim_c)):
        pct = ruim.mean() * 100
        if pct > MAX_INCONSISTENTES_PCT:
            ex = df.loc[ruim, ["ID PEDIDO", "QTD", f"{nome} UNIT", f"{nome} TOTAL"]].head(5)
            raise ErroDeDados(
                f"{nome}: {pct:.1f}% das linhas não fecham (TOTAL != QTD x UNIT). "
                f"Provável erro de leitura/formatação dos valores em R$ na planilha.\n{ex.to_string(index=False)}"
            )

    df["QTD"] = df["QTD"].round().astype(int)
    df["VENDA"] = df["VENDA TOTAL"]
    df["CUSTO"] = df["CUSTO TOTAL"]
    df["ANO"] = df["DT"].dt.year.astype(str)
    df["ANO_MES"] = df["DT"].dt.to_period("M").astype(str)

    qualidade = {
        "linhas_lidas": lidas,
        "linhas_descartadas": n_inv,
        "ids_pedido_duplicados": int(df["ID PEDIDO"].duplicated().sum()),
    }
    return df, qualidade


# --------------------------------------------------------------------------
# Agregações
# --------------------------------------------------------------------------
def resumir_grupo(dataframe: pd.DataFrame, cols) -> pd.DataFrame:
    g = dataframe.groupby(cols, as_index=False).agg(
        pedidos=("ID PEDIDO", "nunique"),
        qtd=("QTD", "sum"),
        venda=("VENDA", "sum"),
        custo=("CUSTO", "sum"),
    )
    g["lucro"] = g["venda"] - g["custo"]
    g["margem_pct"] = np.where(g["venda"] > 0, g["lucro"] / g["venda"] * 100, 0.0).round(2)
    for c in ("venda", "custo", "lucro"):
        g[c] = g[c].round(2)
    return g


def resumo_mes(df: pd.DataFrame, mes: str):
    sub = df[df["ANO_MES"] == mes]
    if sub.empty:
        return None
    g = resumir_grupo(sub.assign(_k="x"), ["_k"]).drop(columns="_k")
    r = json.loads(g.to_json(orient="records"))[0]  # mantém inteiros como inteiros
    r["mes"] = mes
    return r


def montar_saida(df: pd.DataFrame, qualidade: dict) -> dict:
    venda_total = float(df["VENDA"].sum())
    custo_total = float(df["CUSTO"].sum())
    if venda_total <= 0:
        raise ErroDeDados("Venda total zerada - verifique a planilha.")
    margem_geral = round((venda_total - custo_total) / venda_total * 100, 2)

    por_ano = resumir_grupo(df, ["ANO"]).sort_values("ANO")
    por_mes = resumir_grupo(df, ["ANO_MES"]).sort_values("ANO_MES").reset_index(drop=True)
    por_mes["venda_var_pct"] = (por_mes["venda"].pct_change() * 100).round(2)
    por_categoria = resumir_grupo(df, ["CATEGORIA"]).sort_values("venda", ascending=False)
    por_produto = resumir_grupo(df, ["PRODUTO", "CATEGORIA"]).sort_values("venda", ascending=False).reset_index(drop=True)
    por_regiao = resumir_grupo(df, ["REGIÃO"]).sort_values("venda", ascending=False)

    meses = list(por_mes["ANO_MES"])
    ultimo_mes = meses[-1]
    mes_anterior = meses[-2] if len(meses) > 1 else None
    ult2 = [m for m in (mes_anterior, ultimo_mes) if m]
    ult6 = meses[-6:]

    ultima_data = df["DT"].max()
    mes_parcial = bool(ultima_data < ultima_data + pd.offsets.MonthEnd(0))

    cat_mes_recente = resumir_grupo(df[df["ANO_MES"].isin(ult6)], ["ANO_MES", "CATEGORIA"]).sort_values(["ANO_MES", "CATEGORIA"])
    produto_mes_recente = resumir_grupo(df[df["ANO_MES"].isin(ult2)], ["ANO_MES", "PRODUTO", "CATEGORIA"]).sort_values(["ANO_MES", "venda"], ascending=[True, False])
    regiao_mes_recente = resumir_grupo(df[df["ANO_MES"].isin(ult2)], ["ANO_MES", "REGIÃO"]).sort_values(["ANO_MES", "REGIÃO"])

    # --- Clientes: total + crescimento (janela atual x anterior) ---
    por_cliente = resumir_grupo(df, ["CLIENTE"]).sort_values("venda", ascending=False)
    n = min(6, len(meses) // 2)
    janela_atual = meses[-n:] if n else []
    janela_ant = meses[-2 * n:-n] if n else []
    if n:
        v_at = df[df["ANO_MES"].isin(janela_atual)].groupby("CLIENTE")["VENDA"].sum()
        v_an = df[df["ANO_MES"].isin(janela_ant)].groupby("CLIENTE")["VENDA"].sum()
        cres = pd.concat([v_at.rename("venda_atual"), v_an.rename("venda_anterior")], axis=1).fillna(0.0)
        cres["variacao_rs"] = cres["venda_atual"] - cres["venda_anterior"]
        cres["variacao_pct"] = np.where(cres["venda_anterior"] > 0, cres["variacao_rs"] / cres["venda_anterior"].replace(0, np.nan) * 100, np.nan)
        cres = cres.round(2).reset_index()
        por_cliente = por_cliente.merge(cres, on="CLIENTE", how="left")
        cols = ["CLIENTE", "venda_atual", "venda_anterior", "variacao_rs", "variacao_pct"]
        top_cresc = cres[cres["variacao_rs"] > 0].sort_values("variacao_rs", ascending=False).head(10)[cols]
        top_queda = cres[cres["variacao_rs"] < 0].sort_values("variacao_rs").head(10)[cols]
    else:
        top_cresc = top_queda = pd.DataFrame(columns=["CLIENTE"])

    # --- Cards iniciais (determinísticos) ---
    relevantes = por_produto[por_produto["venda"] >= venda_total * 0.03]
    if relevantes.empty:
        relevantes = por_produto
    pior = relevantes.sort_values("margem_pct").iloc[0]
    rank_pior = int(por_produto.index[por_produto["PRODUTO"] == pior["PRODUTO"]][0]) + 1
    comp = "abaixo" if pior["margem_pct"] < margem_geral else "acima"
    melhor_cat = por_categoria.iloc[0]
    insight_1 = (
        f"{pior['PRODUTO']} é o {rank_pior}º em faturamento ({brl(pior['venda'])}) e tem a menor margem "
        f"entre os produtos relevantes: {pior['margem_pct']:.1f}% ({comp} da média de {margem_geral:.1f}%)."
    )
    insight_2 = (
        f"{melhor_cat['CATEGORIA']} representa {melhor_cat['venda'] / venda_total * 100:.1f}% da receita "
        f"({brl(melhor_cat['venda'])}) com margem de {melhor_cat['margem_pct']:.1f}%."
    )

    def recs(d):
        return json.loads(d.to_json(orient="records", force_ascii=False))

    return {
        "atualizado_em": pd.Timestamp.now(tz="America/Sao_Paulo").strftime("%d/%m/%Y %H:%M"),
        "insights_iniciais": {"insight1": insight_1, "insight2": insight_2},
        "periodo": {
            "inicio": df["DT"].min().strftime("%Y-%m-%d"),
            "fim": ultima_data.strftime("%Y-%m-%d"),
            "ultimo_mes": ultimo_mes,
            "mes_anterior": mes_anterior,
            "mes_parcial": mes_parcial,
            "janela_atual": janela_atual,
            "janela_anterior": janela_ant,
        },
        "qualidade": qualidade,
        "geral": {
            "pedidos": int(df["ID PEDIDO"].nunique()),
            "qtd": int(df["QTD"].sum()),
            "venda": round(venda_total, 2),
            "custo": round(custo_total, 2),
            "lucro": round(venda_total - custo_total, 2),
            "margem_pct": margem_geral,
            "clientes_unicos": int(df["CLIENTE"].nunique()),
        },
        "resumo_ultimo_mes": resumo_mes(df, ultimo_mes),
        "resumo_mes_anterior": resumo_mes(df, mes_anterior) if mes_anterior else None,
        "por_ano": recs(por_ano),
        "por_mes": recs(por_mes),
        "por_categoria": recs(por_categoria),
        "por_produto": recs(por_produto),
        "por_regiao": recs(por_regiao),
        "cat_mes_recente": recs(cat_mes_recente),
        "produto_mes_recente": recs(produto_mes_recente),
        "regiao_mes_recente": recs(regiao_mes_recente),
        "clientes_maior_crescimento": recs(top_cresc),
        "clientes_maior_queda": recs(top_queda),
        "clientes": recs(por_cliente),
    }


def gravar(saida: dict, caminho: str) -> None:
    texto = json.dumps(saida, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                       default=lambda o: o.item() if hasattr(o, "item") else str(o))
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp, caminho)  # escrita atômica: nunca deixa um JSON pela metade


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="lê de um CSV local em vez do Google Sheets")
    ap.add_argument("--saida", default=SAIDA)
    args = ap.parse_args(argv)
    try:
        bruto = ler_csv(args.csv) if args.csv else ler_planilha()
        df, qualidade = preparar(bruto)
        saida = montar_saida(df, qualidade)
        gravar(saida, args.saida)
    except ErroDeDados as e:
        print(f"ERRO: {e}", file=sys.stderr)
        return 1
    g = saida["geral"]
    print(f"{args.saida} atualizado! {qualidade['linhas_lidas']} linhas lidas | "
          f"venda {brl(g['venda'])} | margem {g['margem_pct']}% | "
          f"{saida['periodo']['inicio']} a {saida['periodo']['fim']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
