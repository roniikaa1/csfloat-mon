import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import NoSuchWindowException, TimeoutException
import time
import sys

CHECK_INTERVAL = 10  # Másodperc a frissítések között

def main():
    print("🚀 CSFloat Monitor Indítása...")
    
    options = uc.ChromeOptions()
    # options.add_argument('--headless') # Teszteléshez kikapcsolva, hogy lásd az ablakot
    
    print("🔧 Böngésző indítása (Chrome 153)...")
    driver = uc.Chrome(options=options, version_main=153)
    
    try:
        print("⏳ Várakozás a Chrome inicializálására...")
        time.sleep(3)
        
        # Alapértelmezett szűrő: Rifle, Crown (Foil) matrica, Legújabbak elöl
        # Ezt bármikor lecserélheted a saját linkedre!
        target_url = "https://csfloat.com/search?sort_by=lowest_price&min_float=0.06&max_float=0.8&type=buy_now&def_index=7&paint_index=14&stickers=%5B%7B%22c%22:%22C393%22%7D%5D"
        print(f"🌐 Oldal megnyitása: {target_url}")
        
        driver.get(target_url)
        time.sleep(5) # Kis várakozás az első betöltésnél
        
        while True:
            print(f"\n🔍 Oldal frissítése... ({time.strftime('%H:%M:%S')})")
            driver.refresh() 
            
            try:
                wait = WebDriverWait(driver, 10)
                wait.until(lambda d: d.title != "")
                
                time.sleep(3) # Várunk 3 másodpercet, hogy a fegyverkártyák biztosan betöltsenek a frissítés után
                
                # Megkeressük a fegyver kártyákat az oldalon
                item_cards = driver.find_elements(By.CSS_SELECTOR, "item-card")
                
                if item_cards:
                    print(f"✅ Legfrissebb találatok:")
                    for card in item_cards[:5]: # Csak a legfelső 5 darabot nézzük (a legújabbakat)
                        try:
                            # Fegyver neve és ára
                            name = card.find_element(By.CSS_SELECTOR, ".item-name").text
                            price = card.find_element(By.CSS_SELECTOR, ".price").text
                            print(f" 🔫 {name.ljust(30)} | 💰 {price}")
                        except:
                            pass # Ha valami hiányzik egy kártyáról, ugrunk a következőre
                else:
                    print("⚠️ Nem találtam fegyvereket az oldalon.")
                    
                print("-" * 50)
                
            except TimeoutException:
                print("⚠️ Lassan tölt be az oldal, de folytatjuk a figyelést...")
                
            print(f"⏳ Várakozás {CHECK_INTERVAL} másodpercig a következő ellenőrzésig...")
            time.sleep(CHECK_INTERVAL)
            
    except NoSuchWindowException:
        print("\n❌ A böngészőablak bezárult. Monitorozás leállítva.")
        sys.exit()
    except Exception as e:
        print(f"\n❌ Váratlan hiba történt: {e}")
    finally:
        print("🧹 Háttérfolyamatok leállítása...")
        try:
            driver.quit()
        except:
            pass

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Monitorozás leállítva (Ctrl+C). Szép napot!")
        sys.exit()