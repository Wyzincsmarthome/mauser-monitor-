import os, json, re, time
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import yaml

STATE_FILE = Path("data/state.json")
CONFIG_FILE = Path("config/mauser.yaml")

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
MAUSER_USER = os.getenv("MAUSER_USERNAME")
MAUSER_PASS = os.getenv("MAUSER_PASSWORD")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WyzincPriceWatcher/1.0; +https://wyzinc.pt)"
}

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_discord_message(content: str):
    if not DISCORD_WEBHOOK:
        print("[WARN] DISCORD_WEBHOOK_URL não definido. Mensagem:", content)
        return
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print("[ERROR] Falha ao enviar para Discord:", e)

def get_hidden_inputs(soup: BeautifulSoup):
    data = {}
    for inp in soup.select("input[type=hidden]"):
        name = inp.get("name")
        val = inp.get("value", "")
        if name:
            data[name] = val
    return data

def login_mauser(session: requests.Session, cfg: dict):
    login_cfg = cfg["login"]
    # 1) GET login page para obter tokens ocultos (CSRF/form_key)
    r = session.get(login_cfg["login_page"], headers=HEADERS, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    payload = get_hidden_inputs(soup)

    # 2) preencher user e pass
    payload[login_cfg["user_field"]] = MAUSER_USER
    payload[login_cfg["pass_field"]] = MAUSER_PASS

    # 3) POST login
    r2 = session.post(login_cfg["post_url"], data=payload, headers=HEADERS, timeout=60, allow_redirects=True)
    r2.raise_for_status()

    # 4) Verificação simples de login (heurística)
    # Se continuar a mostrar form de login, assumimos falha
    check = session.get(login_cfg["login_page"], headers=HEADERS, timeout=60)
    if "logout" in check.text.lower() or "sair" in check.text.lower() or "minha conta" in check.text.lower():
        print("[INFO] Login bem-sucedido.")
        return True
    print("[WARN] Não foi possível confirmar login. Continuando mesmo assim...")
    return True

def extract_with_selector(soup: BeautifulSoup, selector: str, regex: str | None):
    el = soup.select_one(selector) if selector else None
    if not el:
        return None
    text = el.get_text(strip=True)
    if regex:
        m = re.search(regex, text)
        if m:
            return m.group(1)
    return text

def normalize_price(val: str | None):
    if not val:
        return None
    # trocar vírgula por ponto e remover espaços/símbolos
    v = val.replace("€", "").replace(" ", "").replace("\u00a0", "")
    v = v.replace(",", ".")
    try:
        return round(float(v), 2)
    except:
        return None

def fetch_product(session: requests.Session, pconf: dict):
    url = pconf["url"]
    r = session.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    price_cfg = pconf.get("price", {})
    stock_cfg = pconf.get("stock", {})

    raw_price = extract_with_selector(soup, price_cfg.get("selector"), price_cfg.get("regex"))
    raw_stock = extract_with_selector(soup, stock_cfg.get("selector"), stock_cfg.get("regex"))

    price = normalize_price(raw_price)
    stock = raw_stock if raw_stock else None

    return {
        "url": url,
        "name": pconf.get("name") or url,
        "price": price,
        "raw_price": raw_price,
        "stock": stock
    }

def diff_values(old: dict | None, new: dict):
    changes = []
    if old is None:
        changes.append("novo_registo")
        return changes
    if old.get("price") != new.get("price"):
        changes.append(f"preço: {old.get('price')} → {new.get('price')}")
    if old.get("stock") != new.get("stock"):
        changes.append(f"stock: {old.get('stock')} → {new.get('stock')}")
    return changes

def main():
    # valida env
    if not (MAUSER_USER and MAUSER_PASS):
        raise RuntimeError("Define MAUSER_USERNAME e MAUSER_PASSWORD em Secrets/ENV.")
    cfg = load_config()
    state = load_state()

    with requests.Session() as s:
        s.headers.update(HEADERS)

        # LOGIN
        ok = login_mauser(s, cfg)
        if not ok:
            send_discord_message(":warning: Falha no login ao fornecedor (Mauser). Verifica credenciais.")
            return

        # LOOP produtos
        changes_msgs = []
        for p in cfg["products"]:
            try:
                data = fetch_product(s, p)
                pid = data["url"]  # chave
                previous = state.get(pid)
                changes = diff_values(previous, data)
                state[pid] = data
                if changes:
                    # Mensagem por produto com alterações
                    msg = (f"**[{p.get('name') or 'Produto'}]**\n"
                           f"{data['url']}\n"
                           f"Alterações: " + "; ".join(changes))
                    changes_msgs.append(msg)
                time.sleep(1.0)  # para não abusar
            except Exception as e:
                err = f":x: Erro ao ler {p.get('name') or p.get('url')}: {e}"
                print(err)
                changes_msgs.append(err)

        # guardar estado
        save_state(state)

        # notificação agregada
        if changes_msgs:
            content = ":bell: **Alterações detetadas (Mauser)**\n\n" + "\n\n".join(changes_msgs)
        else:
            content = ":white_check_mark: Sem alterações em preço/stock (Mauser)."
        send_discord_message(content)

if __name__ == "__main__":
    main()
