import os
import json
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# --- CONFIGURAÇÃO ---
SPREADSHEET_ID = '1Gm8w8-hMVUp4zYV-W4fQ7J_T2alPf4zsR1DynwLRKNM'  # O código que fica entre /d/ e /edit na URL da planilha
ABA_DADOS = 'Dados'                                # Nome exato da aba com as 10.000 linhas

def conectar_sheets():
    scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    
    # Se estiver no GitHub Actions, lê do Secret GCP_SA_KEY; senão, lê do arquivo local credentials.json
    sa_json_str = os.environ.get('GCP_SA_KEY')
    if sa_json_str:
        info = json.loads(sa_json_str)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file('credentials.json', scopes=scopes)
        
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(ABA_DADOS)
    
    # get_all_records() já usa a primeira linha como cabeçalho
    registros = sheet.get_all_records()
    return pd.DataFrame(registros)

df = conectar_sheets()

def parse_numero(val):
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace('R$', '').strip()
    if not s:
        return 0.0
    # Se tem vírgula e ponto (ex: "1.250,50"), remove ponto de milhar e troca vírgula por ponto
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0

df['VENDA'] = df['VENDA TOTAL'].apply(parse_numero)
df['CUSTO'] = df['CUSTO TOTAL'].apply(parse_numero)
df['QTD'] = pd.to_numeric(df['QTD'], errors='coerce').fillna(0).astype(int)

# Trata datas tanto no formato M/D/YYYY quanto DD/MM/YYYY
df['DT'] = pd.to_datetime(df['DATA'], format='%m/%d/%Y', errors='coerce')
mask_na = df['DT'].isna()
if mask_na.any():
    df.loc[mask_na, 'DT'] = pd.to_datetime(df.loc[mask_na, 'DATA'], dayfirst=True, errors='coerce')

df['ANO'] = df['DT'].dt.year.astype(str)
df['ANO_MES'] = df['DT'].dt.to_period('M').astype(str)

def resumir_grupo(dataframe, cols):
    g = dataframe.groupby(cols, as_index=False).agg(
        pedidos=('ID PEDIDO', 'count'),
        qtd=('QTD', 'sum'),
        venda=('VENDA', 'sum'),
        custo=('CUSTO', 'sum')
    )
    g['lucro'] = g['venda'] - g['custo']
    g['margem_pct'] = ((g['lucro'] / g['venda']) * 100).fillna(0).round(2)
    g['venda'] = g['venda'].round(2)
    g['custo'] = g['custo'].round(2)
    g['lucro'] = g['lucro'].round(2)
    return g

venda_total = float(df['VENDA'].sum())
custo_total = float(df['CUSTO'].sum())
margem_geral = round(((venda_total - custo_total) / venda_total) * 100, 2) if venda_total > 0 else 0.0

por_ano = resumir_grupo(df, ['ANO']).sort_values('ANO')
por_mes = resumir_grupo(df, ['ANO_MES']).sort_values('ANO_MES')
por_categoria = resumir_grupo(df, ['CATEGORIA']).sort_values('venda', ascending=False)
por_produto = resumir_grupo(df, ['PRODUTO', 'CATEGORIA']).sort_values('venda', ascending=False).reset_index(drop=True)
por_regiao = resumir_grupo(df, ['REGIÃO']).sort_values('venda', ascending=False)
por_cliente = resumir_grupo(df, ['CLIENTE']).sort_values('venda', ascending=False)

ultimos_6_meses = sorted(df['ANO_MES'].dropna().unique())[-6:]
cat_mes_recente = resumir_grupo(
    df[df['ANO_MES'].isin(ultimos_6_meses)],
    ['ANO_MES', 'CATEGORIA']
).sort_values(['ANO_MES', 'CATEGORIA'])

# Cards iniciais automáticos
piores_prod = por_produto[por_produto['venda'] > 100000].sort_values('margem_pct')
pior = piores_prod.iloc[0]
rank_pior = int(pior.name) + 1
melhor_cat = por_categoria.iloc[0]

def brl(v):
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

insight_1 = (
    f"{pior['PRODUTO']} é o {rank_pior}º em faturamento ({brl(pior['venda'])}), "
    f"mas opera com margem de {pior['margem_pct']:.1f}% (abaixo da média de {margem_geral:.1f}%)."
)
insight_2 = (
    f"{melhor_cat['CATEGORIA']} representa {(melhor_cat['venda']/venda_total)*100:.1f}% da receita "
    f"({brl(melhor_cat['venda'])}) com margem média de {melhor_cat['margem_pct']:.1f}%."
)

saida = {
    "atualizado_em": pd.Timestamp.now(tz='America/Sao_Paulo').strftime('%d/%m/%Y %H:%M'),
    "insights_iniciais": {
        "insight1": insight_1,
        "insight2": insight_2
    },
    "geral": {
        "pedidos": int(len(df)),
        "qtd": int(df['QTD'].sum()),
        "venda": round(venda_total, 2),
        "custo": round(custo_total, 2),
        "lucro": round(venda_total - custo_total, 2),
        "margem_pct": margem_geral,
        "clientes_unicos": int(df['CLIENTE'].nunique())
    },
    "por_ano": por_ano.to_dict(orient='records'),
    "por_mes": por_mes.to_dict(orient='records'),
    "por_categoria": por_categoria.to_dict(orient='records'),
    "por_produto": por_produto.to_dict(orient='records'),
    "por_regiao": por_regiao.to_dict(orient='records'),
    "cat_mes_recente": cat_mes_recente.to_dict(orient='records'),
    "clientes": por_cliente.to_dict(orient='records')
}

with open('dados_ia.json', 'w', encoding='utf-8') as f:
    json.dump(saida, f, ensure_ascii=False, indent=2)

print(f"dados_ia.json atualizado com sucesso! ({len(df)} linhas processadas)")
