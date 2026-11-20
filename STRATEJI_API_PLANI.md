# Strateji API’si ve dashboard strateji seçimi

## Hedef ve kapsam

Bir kullanıcı kendi Python stratejisini yazabilecek, yerel dashboard başlatılırken kaydedebilecek ve **Strateji** menüsünden seçip aynı ASELS PCAP kaydı üzerinde çalıştırabilecek. Mevcut **Çift taraflı kotasyon** varsayılan strateji olarak kalacak ve yeni API’nin çalışan örneği olacak. Ayrı bir **Tek taraflı alış** örneği, aynı motorun farklı kurallar içeren bir stratejiyi motor kaynak koduna dokunmadan çalıştırdığını gösterecek.

İlk sürüm Borsa İstanbul Pay Piyasası, ASELS ve sürekli işlem fazında pasif Day limit emirlerini kapsıyor. Her tarafta bir açık emir ve bir bekleyen istek sınırı uygulanacak. Stratejiler bu kapsamda deftere, kendi emirlerine ve pozisyonlarına göre alış, satış, değiştirme ve iptal kararları verebilecek. Çoklu pay, aynı tarafta birden çok emir, agresif emir türleri, ağ üzerinden OUCH ve tarayıcıdan kod yükleme ayrı geliştirme başlıkları. [Ana geliştirme planı](GELISTIRME_PLANI.md)

## Motor ve strateji sözleşmesi

Bugün `Simulation._reconcile()` hem kotasyon kararını hem OUCH isteklerini oluşturuyor. Bu işlevdeki karar kuralları yerleşik stratejiye taşınacak. Yeni stratejilerin emir kararları mevcut zamanlayıcıya, OUCH codec’ine, kuyruk modeline ve portföye aynı motor üzerinden ulaşacak. [Replay ve simülasyon motoru](dashboard/simulation.py#L179) · [Mevcut kotasyon kararı](dashboard/simulation.py#L317)

### Python API v1

Aşağıdaki imza önerilen genel arayüzü gösteriyor. Tipler strateji geliştiricisine tek bir Python modülünden sunulacak.

```python
class Strategy(Protocol):
    def on_event(
        self,
        event: MarketEvent,
        view: StrategyView,
        updates: tuple[OrderUpdate, ...],
    ) -> tuple[OrderIntent, ...]: ...
```

| Tip | İçerik ve davranış |
|---|---|
| `MarketEvent` | Uygulanan ITCH olayının türü, sıra numarası, sanal borsa zamanı ve türüne göre fiyat veya miktar gibi alanlar. Olay verisi yalnızca replay’in o ana kadar uyguladığı kayıtlardan gelir. |
| `StrategyView` | ASELS ve işlem fazı, en iyi alış ve satış fiyatı ile miktarı, fiyat ölçeği, geçerli fiyat adımı kuralları, lot, pozisyon ve kullanıma açık taraf kapasitesi. Açık emirler için sabit bir emir kimliği, fiyat, kalan ve gerçekleşen miktar bulunur. Bekleyen istekler taraf bazında görülebilir. Görünüm salt okunurdur. |
| `OrderUpdate` | Kabul, ret, değiştirme, iptal ve gerçekleşme bildirimleri. Sıralı motor bildirimi, sanal zaman, ilgili emir veya istek kimliği, miktar ve varsa neden kodu bir kez teslim edilir. |
| `OrderIntent` | `Enter(side, price, quantity)`, `Replace(order_id, price, remaining_quantity)` veya `Cancel(order_id)`. Boş demet emir vermeme kararıdır. Motor kararları döndürüldükleri sırayla ele alır. |

Fiyatlar ham tamsayı ve ölçekle taşınır. Örneğin `12345` ve ölçek `2`, 123,45 ₺ anlamına gelir. Miktar pay adedi olarak tamsayıdır ve lot katı kontrolü motordadır. Fiyat adımı bantları ve komşu kademe hesabı için mevcut doğrulama kuralları yeniden kullanılır. Stratejiye dashboard’un kayan noktalı ekran değerleri veya tüm tarihsel defterin her olayda bir kopyası verilmez.

Motor, açık durumu değiştiren tek bileşendir. İşlem fazını, fiyat adımını, lotu, pozisyon kapasitesini, emir sahipliğini ve pasiflik koşulunu denetler. Fiyatın karşı piyasa emrine veya kendi karşı emrine temas etmesi engellenir. OUCH token’larını, istek ve yanıt sırasını, sanal gecikmeyi, gerçekleşmeleri ve komisyonu motor yönetir. İş kuralı ihlalleri ilgili istekle eşlenebilen bir ret üretir. `Replace`, istenen kalan miktarı gönderim anındaki kümülatif gerçekleşmeyle toplayıp prototipteki OUCH toplam miktarına çevirir. İstek yoldayken oluşan yeni gerçekleşmeler varıştaki kalan miktarı azaltır.

### Karar zamanı ve oturum yaşam döngüsü

1. Motor sıradaki ITCH olayından kesin olarak önce zamanı gelen OUCH isteklerini işler. Eşit zaman damgasında ITCH önceliklidir. Aynı zamanda gelen OUCH istekleri gönderim sırasını korur.
2. ITCH olayı deftere uygulanır ve varsa o olaya bağlı gerçekleşmeler portföye işlenir. Etkin strateji, güncel görünümü ve son karar çağrısından bu yana biriken sıralı emir bildirimlerini alır. Eşit zaman damgalı ITCH olayları ayrı ayrı görünür. Bildirim işlenirken tekrar iç içe strateji çağrısı yapılmaz.
3. Strateji bir karar grubunu döndürür. Motor önce grubun biçimini ve taraf başına istek sınırını kontrol eder, ardından geçerli niyetleri mevcut OUCH yoluna verir. Seans ve piyasa risk kontrolleri emir varışında da uygulanır. Strateji arayüzünde bağımsız duvar saati zamanlayıcısı yoktur. Kararlar, örneğin kotasyon yenileme aralığı, sanal borsa zamanını kullanır.
4. Duraklatma ve hız değişikliği strateji örneğini ve sanal emir sırasını korur. Strateji kapanırken, faz sona ererken, defter temizlenirken ve seans biterken motor açık emirleri geri çeker ve önceki nesilden bekleyen isteklerin sonradan emir açmasını engeller. Motor karar aralığını yeniden başlatan bu geçişi strateji görünümünde bildirir. Reset yeni bir strateji örneği oluşturur ve koşunun portföyünü, emir günlüğünü ve defterini baştan kurar. Son olaydan sonraki iptal ve retler motorun sonuç günlüğüne yazılır.
5. Strateji istisna atarsa veya sözleşme dışı bir sonuç döndürürse motor o karar grubundan emir göndermez. Açık emirler geri çekilir, bekleyen istekler geçersizleştirilir ve koşu hatayla durur. Dashboard strateji kimliğini, son işlenen ITCH sırasını ve okunabilir hata özetini gösterir. Önceki gerçekleşmeler ve portföy hata incelemesi için görünür kalır.

Kotasyon örneğine geçişte mevcut karar sıklığı, açık emrin kısmi gerçekleşme sonrası aynı fiyatta yeniden doldurulmaması ve seans geçişleri OUCH bayt dizisi karşılaştırılarak korunacak. Tekrarlanabilir stratejiler, sistem saatine veya dış girdilere bağlı kararlar yerine motorun sunduğu olayları ve sanal zamanı kullanır. [OUCH gönderim ve yanıt kodu](dashboard/simulation.py#L256) · [ITCH işleme sırası](dashboard/simulation.py#L457)

## Strateji kaydı ve kullanıcı akışı

Her strateji kaydında sabit `id`, strateji sürümü, uyumlu API sürümü, Türkçe ad ve açıklama, parametre şeması ve koşu örneğini oluşturan bir fabrika bulunur. Şema her parametre için tür, birim, varsayılan değer, geçerli aralık veya seçenekler ve **i** açıklamasını tanımlar. Sunucu tipi, sınırları ve parametreler arası kuralları denetler. Örneğin kotasyon miktarı, koşunun pozisyon limitini aşamaz.

| Ayarlar | Örnekler |
|---|---|
| Ortak simülasyon ayarları | Pozisyon limiti, simüle OUCH gecikmesi, komisyon. Her stratejiye aynı motor kuralları uygulanır. |
| Çift taraflı kotasyon ayarları | Emir miktarı, kotasyon yenileme aralığı, en iyi kademeye uzaklık. Mevcut varsayılanlar aktarılır. |
| Tek taraflı alış örneğinin ayarları | Emir miktarı ve karar aralığı. Örnek, aynı API’den pasif alış emri verir ve koşul bozulunca iptal eder. |

Yerel geliştirici akışı, kullanıcının kendi Python modülünde bir strateji sınıfı ve kayıt tanımlarını döndüren `get_strategies()` işlevi yazmasıyla başlayacak. Önerilen başlatma biçimi `python -B -m dashboard --strategy-module my_strategies.asels`. Seçenek tekrarlanabilir ve yalnızca başlatan kişinin açıkça belirttiği güvenilir yerel modülleri yükler. Yerleşik stratejiler seçeneksiz kullanılabilir. API sürümü uyumsuzsa veya bir kimlik çakışıyorsa sunucu, sorunu adlandıran bir başlangıç hatası verir.

Web istekleri kayıtlı strateji kimliği ve veri türü doğrulanan parametreleri taşır. Kullanıcının yerel Python modülü, başlatan kullanıcının işletim sistemi yetkileriyle çalışır. İlk ürün teslimatı bu güven modelini kullanan yerel bir kurulumdur. Bir barındırılan sürümde kullanıcıların kendi kodunu yükleyebilmesi için işletim sistemi seviyesinde dosya ve ağ izolasyonu, kaynak sınırları ve süre aşımı olan ayrı bir strateji çalıştırıcısı gerekir. Bu özellik yerel strateji seçimi tamamlandıktan sonra planlanır.

## Dashboard ve HTTP akışı

1. Dashboard, **Strateji** menüsünde kayıtlı seçenekleri ve açıklamalarını gösterir. İlk açılışta **Çift taraflı kotasyon** seçilidir. Seçilen stratejinin formu şemadan üretilir. Ortak simülasyon ayarları ayrı başlıkta kalır. Var olan **i** yardım bileşeni her alanın birimini ve etkisini açıklar.
2. Kullanıcı strateji ve ayarları kaydı yüklemeden önce veya replay hazırken uygular. İlk sürümde strateji kimliği ve koşu ayarları replay başladıktan sonra sabitlenir. Değiştirmek için **Başa al** kullanılır. Hız, başlatma, duraklatma ve stratejiyi açma veya kapatma kontrolleri çalışmaya devam eder.
3. Geçerli seçim tek işlemde kaydedilir. Bilinmeyen strateji, geçersiz ayar veya başladıktan sonra gelen seçim isteğinde mevcut durum korunur. Sunucu bu koşulu kendisi doğrular. Panel aynı durumda seçiciyi devre dışı bırakıp **Başa al** seçeneğini gösterir.
4. Strateji kimliği ve sürümü ile kullanılan ayarlar koşu durumunda görünür. Pozisyon, kâr/zarar, açık alış ve satış emirleri, OUCH günlüğü ve iki taraflı emir defteri alanları seçilen stratejinin sonuçlarını gösterir. Elle açma ve kapatma, tekrar inceleme için uygulandığı ITCH sırasıyla günlüğe yazılır. Strateji değişikliği PCAP olay önbelleğini yeniden oluşturmaz.

| İstek veya alan | Önerilen veri |
|---|---|
| `GET /api/strategies` | Kayıtlı kimlikler, strateji ve API sürümleri, adlar, açıklamalar, parametre şemaları ve varsayılan strateji kimliği. |
| `POST /api/control` | `{"action":"select_strategy","strategy_id":"two_sided_quote","params":{"quantity":10}}`. Eksik parametreler o stratejinin varsayılanıyla doldurulur. Ortak ayarlar mevcut `config` kontrolünden doğrulanır. |
| `GET /api/state` | Mevcut `strategy` sonuç alanının yanında `strategy_selection` altında kimlik, sürüm ve uygulanmış parametreler. Hata durumunda ilgili strateji ve olay sıra bilgisi. |

Seçim ve replay başlatma, mevcut `Controller.condition` altında sıraya alınır. Form etiketi ve alan değerlerini şema türüne göre gösterir. Tamsayı, boolean ve seçenek alanlarının türü JSON’da korunur. [Controller](dashboard/server.py#L73) · [Dashboard HTML](dashboard/static/index.html#L102) · [Mevcut form ve durum akışı](dashboard/static/app.js#L226)

## Uygulama sırası

| Aşama | İş ve teslimat | Tamamlanma ölçütü |
|---|---|---|
| 1. Mevcut davranışı kaydet | Sentetik fixture ve yerel gerçek ASELS kaydıyla OUCH sırası, gerçekleşmeler, son portföy ve Maks replay süresini kaydet. Tam emir dizisi için testte akış özeti al. | Refaktör öncesi karşılaştırılabilir sonuç ve performans tabanı var. Ekrandaki son mesajlarla sınırlı bir karşılaştırma kullanılmıyor. |
| 2. API ve hazır strateji | API tiplerini ve motor adaptörünü ekle. Mevcut kotasyonu `dashboard/strategies` altında örnek modüle çıkar. Motorun doğrudan kullanımında hazır strateji varsayılan kalsın. | Aynı kayıt ve ayarlarla tam OUCH çerçeve dizisi, gerçekleşme günlüğü ve son portföy eski sonuçlarla eşleşiyor. |
| 3. Kayıt ve harici örnek | Sürümlü strateji kaydı, şema doğrulaması ve yerel modül seçeneğini ekle. Tek taraflı alış örneğini bu yolla çalıştır. | Başka bir kullanıcı yalnızca örnek Python dosyasını oluşturarak stratejiyi kaydedip simüle edebiliyor. Her koşuda yeni örnek alınıyor. |
| 4. Dashboard seçimi | Listeleme, seçim ve durum uçlarını ekle. Parametre formunu ve Türkçe yardımları dinamik yap. Hazır ve çalışan oturum durumları için kontrolleri bağla. | İki farklı strateji aynı kayıt üzerinde seçilip oynatılabiliyor. Seçim, kâr/zarar ve emirlerin hangi koşuya ait olduğu ekranda açık. |
| 5. Doğrulama ve kullanım | Entegrasyon, hata ve hız testlerini çalıştır. README’ye strateji yazma, kaydetme, seçme ve birimleri açıklayan kısa başlangıç örneği ekle. | Temiz yerel kurulumdan örnek strateji görülebiliyor ve çalıştırılabiliyor. Mevcut testler ile yeni strateji testleri geçiyor. |

## Kabul testleri

- **Tekrarlanabilirlik.** Aynı olay, strateji sürümü ve parametrelerle sentetik kaydı 1×, 1.000× ve Maks’ta çalıştır. Hız ve aradaki duraklatmalar OUCH mesajlarını, gerçekleşmeleri veya son portföyü değiştirmemeli. Gerçek ASELS kaydında varsayılan stratejinin eski ve yeni Maks koşusunu aynı veri ve ayarla karşılaştır. Tam mesaj ve fill dizisinin akış özeti alınmalı.
- **Emir kuralları.** Sıfır sanal OUCH gecikmesinde eşit zaman damgası, aynı tarafta bekleyen istek, hatalı lot ve fiyat, pozisyon sınırı, kendi karşı emrine temas, kısmi gerçekleşme sonrası `Replace`, kuyruk önceliği ve faz geçişini dene. İptal veya reset sonrası eski istek yeni emir açmamalı.
- **Özel strateji ve hata.** Harici bir örnek motor dosyalarına dokunmadan kaydolmalı, alış ve iptal üretmeli ve yalnızca o ana kadar görünür olayları almalı. Parametre ve API sürümü hataları anlaşılır olmalı. Karar sırasında istisna oluştuğunda açık emirler çekilmeli, portföy görünür kalmalı ve reset temiz örnek başlatmalı.
- **Panel.** Varsayılan seçim, şemadan gelen alan ve yardımlar, bilinmeyen kimlik, hatalı parametre, koşu ortasında seçim talebi, iki strateji arasında reset ile geçiş ve çalışırken hız değişikliği doğrulanmalı.
- **Performans.** Hazırlanmış aynı ASELS olay önbelleğinde, aynı makinede ve Maks kipte refaktör öncesi ve sonrası en az beş koşunun medyan replay süresi ve olay/s değeri karşılaştırılmalı. İlk hedef varsayılan stratejide medyan sürenin en fazla %15 artması. Strateji çağrısı maliyeti ölçülürse C++ PCAP işleme gecikmesinden ve gerçek ağ OUCH süresinden ayrı raporlanmalı.

[Mevcut OUCH ve simülasyon testleri](tests/test_simulation.py) · [Mevcut dashboard testleri](tests/dashboard_test.py) · [Başlangıç performans raporu](BENCHMARK.md)
