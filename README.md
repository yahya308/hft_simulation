# BISTECH Pay Piyasası HFT simülatörü

Proje Borsa İstanbul Pay Piyasası için çevrimdışı replay ve tekli veya çoklu pay üzerinde kotasyon simülasyonu sunar. Dashboard PCAP, replay, strateji ve sonuçları aynı ekrandan yönetir. `hft_replay` kanal seviyesinde denetim ve TSV çıktısı için komut satırı aracı olarak kullanılabilir. [Geliştirme planı](GELISTIRME_PLANI.md)

## Nasıl çalışır?

1. PCAP dosyasında verdiğin IP ve UDP portuna giden paketleri bulur.
2. MoldUDP64 sıralarını doğrular ve ITCH mesajlarını okur. Dashboard, aynı oturuma ait yedek PCAP verilirse eksik aralığı arşivden doldurur. Doğrulanamayan boşlukta replay başlamaz.
3. Emir ekleme, gerçekleşme ve silme mesajlarını deftere uygular. Dosyanın sonunda kalan fiyatlı emirleri yazar. Aynı fiyattaki emirlerde BIST'in sıralama zamanı ve sıra numarası kullanılır.

Dashboard, C++ okuyucusundan gelen seçili hisse olaylarını ortak bir sanal piyasa saatinde işler. Strateji her hisse için ayrı bir örnek olarak çalışır ve simüle edilmiş borsaya BISTECH OUCH emirleri gönderir. Gerçekleşmeler ilgili hissenin yerel test portföyüne yazılır.

## Derleme ve test

CMake 3.20+, Ninja ve C++20 derleyicisiyle derle. Dashboard ve testler Python 3.10+ kullanır. Dashboard HTTP testi de yerel `127.0.0.1` bağlantısıyla çalışır.

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

## Pay Piyasası dashboard

```powershell
python -B -m dashboard
```

[Dashboard’u aç](http://127.0.0.1:8765/). `--port 8766` gibi bir seçenekle yerel portu değiştirebilirsin.

1. **PCAP kaydı** alanından dosyayı seç ve **Hisseleri listele** düğmesine bas. `20260522.pcap` için hedef `233.113.216.71`, başlangıç portu `21001`. Bu kayıt için 21001–21004 kanallarındaki açılış referans verisinden uygun paylar listelenir. Bir veya birkaç hisse işaretleyip **Kaydı yükle** düğmesine bas. Seçili hisselerin olayları kanal başına `build/dashboard-cache` içinde önbelleğe alınır.
2. **Sürekli işleme ilerle**, seçili hisselere ait olayları sırayla uygulayıp odaktaki hisse sürekli işleme geçtiğinde duraklar. **Başlat** ve **Duraklat** ile devam edebilirsin. **Başa al**, seçili hisselerin defterlerini, portföylerini ve emir günlüklerini sıfırlar.
3. **1×**, bir saniyelik piyasa süresini bir saniyede oynatmayı hedefler. **1–1.000×** bekleme sürelerini ölçekler. **Maks** zaman beklemelerini kaldırır ve hızı bilgisayarın işlem kapasitesi belirler. Her kip aynı olay sırasını kullanır.
4. **Strateji** bölümünden **Çift taraflı kotasyon**, **Öğrenen çift taraflı kotasyon** veya **Tek taraflı alış** seç. Stratejinin ayarları ve ortak simülasyon ayarları aynı ekrandan uygulanır. Oynatma başladıktan sonra strateji veya ayar değiştirmek için **Başa al** düğmesini kullan. Stratejiyi açıp kapatma ve hız kontrolü replay sırasında da kullanılabilir. Ekrandaki **i** düğmeleri alanları açıklar.
5. **Oturumdaki hisseler** tablosu her hissenin pozisyonunu, kâr/zararını ve gerçekleşmelerini gösterir. Hisse adına tıklayarak defteri, grafiği ve emir günlüğünü o hisseye odaklayabilirsin. Odak değişirken diğer hisseler aynı replay’de işlemeye devam eder. **Emir defteri** bölümünde **Simüle** veya **Tarihsel ITCH** görünümü seçilebilir.

Her kanalın MoldUDP64 sırası ayrı doğrulanır. Birden fazla kanal seçildiğinde olaylar borsa zamanı ile birleştirilir. Eşit zaman damgasında önce UDP portu, sonra kaynak Mold sırası kullanılır. Strateji ayarları ortak uygulanır. Pozisyon limiti ve emirler hisse bazındadır.

### Gecikme ve işlem öncesi risk

**Strateji → Ortak simülasyon ayarları** altında üç gecikme hattı ayarlanabilir. Gecikmeler ve dalgalanmalar mikrosaniye cinsindendir ve sanal borsa saatine uygulanır.

| Ayar | Sanal başlangıç ve bitiş |
|---|---|
| Piyasa verisi | ITCH olayının deftere uygulanmasından stratejinin o olayın görüntüsünü almasına kadar |
| Emir gidişi | Stratejinin emir kararından emrin eşleştiriciye varmasına kadar |
| OUCH yanıtı | Motorun yanıt üretmesinden stratejinin veya yerel OUCH istemcisinin yanıtı almasına kadar |

Her hat için ayrı dalgalanma seçilebilir. Gecikme, temel sürenin etrafından örneklenir ve sıfırdan küçükse sıfıra çekilir. Aynı hattaki mesaj sırası korunur. Aynı tohum, aynı girdiler ve aynı motor koşulu gecikme dizisini tekrar üretir. Varsayılan ayarda emir gidişi 100 µs, diğer gecikmeler ve dalgalanmalar sıfırdır. İç strateji, zamanı gelen emir yanıtlarını bir sonraki piyasa bildirimiyle alır.

Pay bazındaki pozisyon limitine ek olarak emir miktarı, sanal saniyedeki yeni ve değiştirilen emir sayısı, açık emirleri içeren brüt değer ve toplam zarar sınırı ayarlanabilir. Ek limit için `0` kapalı demektir. Zarar sınırı veya **Acil durdur** açık emirleri iptal eder, bekleyenleri reddeder ve emir girişini yeni koşuya kadar kilitler. Tarihsel replay devam eder. Brüt sınır emir kabul anında değerlendirilir. `POST /api/emergency-stop`, mevcut `run_id` ile alınan durdurma isteğini HTTP `202` ile onaylar. Bu onay strateji çağrısının tamamlanmasını beklemez. Dashboard isteğin alındığını, uygulandığını ve aradaki gerçek süreyi ayrı gösterir. Harici OUCH kipinde motor işlemekte olduğu olayı tamamlayıp durdurmayı sıradaki işlemden önce uygular.

### Veri kapsamı ve yedek kanal

**Yedek ITCH kanalları** isteğe bağlı olarak aynı MoldUDP64 oturumuna ait ikinci bir PCAP veya arşivlenmiş Rewinder trafiği alır. Her eşlemede ana UDP portunu ve yedek IP/portu belirt. Kayıtların ikisi de çalışma alanında olmalı. Kapsam bölümünde kanalın bitişi, tespit edilen boşluk ve geri alınan mesaj sayısı görünür. `capture_prefix` kapanış işareti olmayan doğrulanmış kesittir. Eksik aralık yedekte yoksa hazırlık durur ve ilk eksik sıra gösterilir.

Hazır olay dosyasından tarihsel defteri bağımsız bir Python uygulamasıyla kontrol edebilirsin. Bu kontrol, normalize ITCH mesajlarından çıkarılan emir kimliklerini, bakiyeleri, sıra bilgilerini ve fiyat kademelerini karşılaştırır. Ham paket çözümünü bağımsız olarak doğrulamak için ayrı ayrıştırıcı çıktısı, gerçek gerçekleşmeleri karşılaştırmak için broker kayıtları gerekir.

```powershell
python -B tools/verify_reference_book.py 21001=build/dashboard-cache/OLAY_DOSYASI.ndjson --symbols ASELS.E --output build/reference-check.json
```

### Koşu günlüğü ve tekrarlanabilirlik

Her başlatılan koşu `build/runs/<kimlik>/` altına kaydedilir. `manifest.json` PCAP ve olay dosyası özetlerini, kapsamı, strateji sürümü ve ayarlarını, motor özetini ve portföy sonucunu içerir. `events.ndjson` emir kararlarını, varışları, yanıtları, gerçekleşmeleri, eşleştirme gerekçelerini ve risk olaylarını sıralı kaydeder. Koşu kapandığında dashboard’dan iki dosyayı indirebilirsin. SHA-256 ile kontrol edebilir ve aynı iki koşunun çıktılarını kıyaslayabilirsin. Bağımsız bütünlük kanıtı için özetleri kayıt dizininden ayrı sakla.

```powershell
python -B tools/verify_runs.py build/runs/ILK_KOSU build/runs/TEKRAR_KOSUSU
```

Günlük yazıldıkça her 4.096 kayıtta veya bir saniye geçtikten sonraki ilk yazmada, ayrıca duraklatma ve kapanışta, diske eşitlenir. `checkpoint.json` o ana kadar kaydedilen bölümün uzunluğunu, kayıt sayısını ve SHA-256 özetini tutar. Süreç kesilirse dashboard sonraki açılışta yazarı kalmamış ve doğrulanabilir koşuyu `interrupted` olarak işaretler ve kimliğini gösterir. Denetim için yalnız son kontrol noktasına kadar doğrulanan bölümü ayrı bir dizine çıkarabilirsin. Son kontrol noktasından sonraki satırlar diske ulaşmamış olabilir.

```powershell
python -B tools/verify_runs.py build/runs/KOSU --inspect
python -B tools/verify_runs.py build/runs/KOSU --recover-to build/recovered/KOSU
```

Tamamlanmış harici OUCH koşusunu kayıtlı komutlardan tekrar çalıştırmak için her UDP kanalı başına koşuda kullanılan aynı olay dosyasını ver. Araç olay dosyalarının ve motor kaynaklarının SHA-256 özetini doğrular. Kaydedilmiş komutları ve manuel kontrolleri aynı sanal zaman ve sıra sınırlarında uygular. Üretilen sıralı ikili OUCH yanıtlarını, son portföyü ve emir sayaçlarını kayıtla karşılaştırır.

```powershell
python -B tools/replay_external.py build/runs/KOSU --tape 21001=build/dashboard-cache/OLAY_DOSYASI.ndjson
```

### Harici OUCH oturumu

Yerel bir istemciyle test etmek için ayrı bir SoupBinTCP portu aç ve dashboard’dan **Harici OUCH istemcisi** stratejisini seç. `SIM` varsayılan test kullanıcı adıdır. Parola test oturumu içindir ve en çok 10 ASCII karakter olabilir.

```powershell
$env:HFT_OUCH_PASSWORD = 'SIMTEST1'
python -B -m dashboard --ouch-port 9001 --ouch-user SIM
```

İstemci `127.0.0.1:9001` adresine bağlanır ve SoupBinTCP Login Request ile oturum açar. Tek istemci, sıralı yanıtlar, kalp atışı ve tamponda kalan sıra numarasından yeniden bağlanma desteklenir. Emirler mevcut pasif Day `Enter`, `Replace` ve `Cancel` kapsamıyla motora gider. `Replace` ve `Cancel` ilk `Enter` token’ını kullanır. **Başa al** yeni oturum kimliği üretir. Ortak OUCH hattında emirler, farklı hisselere gitse veya gecikme dalgalanması uygulansa bile giriş sırasını korur. Tarihsel ITCH olayları, aynı sanal zamana denk gelen emir varışlarından önce işlenir. Aktif koşudaki giriş çerçeveleri, sanal zaman damgaları ve yayımlanan yanıt sırası koşu günlüğünde bulunur.

Harici OUCH koşusunda **Piyasa verisi uç noktası** bağlantısı normalleştirilmiş tarihsel ITCH olaylarını yerel HTTP üzerinden JSON olarak verir. Sorgu `GET /api/market-feed?run_id=KOSU&after=0&limit=256` ile başlar. Sonraki istekte `after` için `next_cursor` değerini kullan. Her satırda kaynak UDP portu, özgün Mold sıra numarası, normalleştirilmiş olay dizisi ve olayın istemciye açıldığı sanal zaman `available_time_ns` bulunur. Örneğin `event` dizisinin ilk dört alanı tür, replay sırası, borsa zamanı ve defter kimliğidir. İlk entegrasyon testi için **1×** hızı kullan. İstemci 32.768 olaylık tamponun gerisinde kalırsa HTTP `409` ve `overrun` döner. Baştan eksiksiz okumak için koşuyu yenile. Akış API’si ayrı bir ham ITCH ağ bağlantısı kullanmaz.

### Broker kaydıyla karşılaştırma

Bağımsız bir broker veya test ortamı kaydındaki OUCH yanıtlarını, oturum başına bir CSV dosyasına gerçek yanıt sırasıyla çıkar. Başlık `symbol,token,type,time_ns,price_raw,quantity,reason` olmalı. `A`, `U` ve `E` satırları fiyat ve miktarı, `J` ve `C` satırları gerekçe kodunu taşır. İsteğe bağlı `side`, `previous_token` ve `request_time_ns` alanları karşılaştırmayı ayrıntılandırır. `time_ns` borsanın yanıt zamanıdır. `request_time_ns` aynı zaman tabanına çevrilmiş istek gönderim zamanıdır. İşlem fiyatının tamsayı ölçeği, simüle edilen hissenin ölçeğiyle aynı olmalı. Tamamlanmış bir harici OUCH koşusunda araç, istemciye yayımlanan `A/J/U/C/E` dizisini karşılaştırır ve sıra, eşleşme, fiyat, miktar, zaman ve varsa gidiş gecikmesi farklarını raporlar. Çıkış kodu `0` uyum, `1` fark, `2` girdi veya bütünlük hatasıdır.

```powershell
python -B tools/compare_broker_run.py build/runs/KOSU build/broker-reference.csv --output build/broker-comparison.json
```

### Portföy fiyat duyarlılığı ve stres testi

Dashboard’daki **Portföy fiyat şoku** bölümünde seçili bütün hisselerin referans fiyatına aynı yüzde değişimi uygulayabilirsin. Örneğin `-1` girildiğinde her fiyat %1 azaltılarak portföy yeniden değerlenir. Panel hisse başına etkiyi, mevcut toplam kâr/zararı ve şok sonrası toplamı gösterir. Hesap aynı replay anındaki nakit ve pozisyonlarla yapılır. Referans fiyat, tarihsel ITCH defterinin son geçerli orta fiyatıdır.

$Q_i$ işaretli hisse adedi, $C_i$ simüle nakit bakiyesi, $m_i$ referans fiyat olsun. Portföy kâr/zararı ve fiyatlara göre kısmi türevi şöyledir.

$$
\Pi(\mathbf{m})=\sum_i(C_i+Q_i m_i),
\qquad
\frac{\partial\Pi}{\partial m_i}=Q_i,
\qquad
\nabla_{\mathbf{m}}\Pi=(Q_1,\ldots,Q_n).
$$

Ortak yüzde şoku $s$ ile yeni fiyat $m_i(s)=m_i(1+s/100)$ olur. Zincir kuralı, bir yüzde puanı başına portföy duyarlılığını verir.

$$
\frac{d\Pi}{ds}
=\sum_i\frac{\partial\Pi}{\partial m_i}\frac{dm_i(s)}{ds}
=\sum_i\frac{Q_i m_i}{100},
\qquad
\Delta\Pi=s\sum_i\frac{Q_i m_i}{100}.
$$

Pozisyon sabitken duyarlılık şok boyunca değişmez. Bu nedenle hesap bu senaryo için tam yeniden değerlemeyle eşleşir. Açık bir pozisyonun referans fiyatı eksikse portföy toplamı ve şok etkisi panelde boş bırakılır.

Örneğin ASELS’te −15 pay, referans 410,125 ₺ ve THYAO’da +100 pay, referans 288,125 ₺ iken ortak **−%1** şok ASELS’te **+61,51875 ₺**, THYAO’da **−288,125 ₺** etki verir. Toplam etki **−226,60625 ₺** olur. Başlangıç kâr/zararı −1.440,375 ₺ ise şok sonrası sonuç −1.666,98125 ₺’dir. [Stres hesabı](dashboard/portfolio_risk.py) · [Türev ve yeniden değerleme testleri](tests/test_portfolio_risk.py)

### Test stratejisi ve OUCH

Strateji, seçili payların sürekli işlem fazında en iyi alış ve satış kademelerine iki pasif Day limit emir verir. Fiyatlar değiştiğinde kotasyonları OUCH `Replace` ile günceller. Yerel motor ikili SoupBinTCP veri çerçeveleri içinde `Enter`, `Replace`, `Cancel` isteklerini ve kabul, ret, iptal ve gerçekleşme yanıtlarını işler. Emir günlüğündeki mesaj bilgisinden token, istek/yanıt sırası ve ikili çerçeve görülebilir. Aynı sanal zaman damgasındaki tarihsel ITCH olayı, simüle emir varışından önce işlenir. Harici test istemcisi için isteğe bağlı oturum yerel arayüzde açılır. Mass Quote protokol kapsamı dışındadır. [BISTECH OUCH](https://borsaistanbul.com/files/bistech-ouch-protocol-specification-sr-3-12.pdf)

### Öğrenen çift taraflı kotasyon

Strateji, tarihsel ITCH defterindeki en iyi alış ve satış kademelerinin lot toplamlarından emir dengesizliğini hesaplar. Alış lotu $B$, satış lotu $A$ olmak üzere $I=(B-A)/(B+A)$. Her yeni orta fiyat seviyesindeki ilk geçerli dengesizliği ve modelin o andaki tahminini saklar. Orta fiyat bir sonraki kez değiştiğinde saklanan örneği yukarı hareket için $y=1$, aşağı hareket için $y=0$ ile etiketler. Defter geçersiz hale gelirse veya piyasa evresi değişirse bekleyen örnek silinir.

Model, $z=w_0+w_1 I$ ve $p=\sigma(z)=1/(1+e^{-z})$ ile bir sonraki orta fiyat hareketinin yukarı yönlü olma olasılığını tahmin eder. Lojistik kayıp $L=-y\log p-(1-y)\log(1-p)$ için zincir kuralı şu gradyanı verir.

$$
\frac{\partial L}{\partial w_j}
=\frac{\partial L}{\partial p}\frac{\partial p}{\partial z}\frac{\partial z}{\partial w_j}
=(p-y)x_j,\qquad
(x_0,x_1)=(1,I),\qquad
w_j\leftarrow w_j-\eta(p-y)x_j.
$$

Örneğin en iyi alışta 800, satışta 200 lot varsa $I=0{,}6$ olur. Model sıfır ağırlıkla %50 tahmin yapar. Sonraki orta fiyat yükselirse $y=1$ olur ve gradyan $(-0{,}5,\,-0{,}3)$ çıkar. Öğrenme hızı $\eta=0{,}1$ ile ağırlıklar $(0{,}05,\,0{,}03)$ olur. Aynı defter dengesi tekrar görülürse yükseliş tahmini yaklaşık %51,7’ye çıkar.

Varsayılan ayarla model, 20 etiketli örnekten sonra kotasyona etki eder. Yukarı hareket tahmini en az %55 ise satış kotasyonu bir ek kademe yukarı, en fazla %45 ise alış kotasyonu bir ek kademe aşağı alınır. Kotasyonlar simüle defterin en iyi kademelerini temel alır ve aynı OUCH ve fiyat-zaman motorundan geçer. Model her hisse için ayrı öğrenir ve her yeni replay koşusuna sıfır ağırlıkla başlar.

Stratejiyi seçip ayarları uyguladıktan sonra **Öğrenme ayrıntıları** bölümünde anlık tahmini, son eğitim örneğinin etiketini, gradyanını, güncelleme öncesi ve sonrası ağırlıklarını ve son kotasyon kararının kademe uzaklıklarını görebilirsin. [Strateji kodu](dashboard/strategies/learning_quote.py) · [Emir dengesizliği ve orta fiyat çalışması](https://arxiv.org/abs/1512.03492)

### Fiyat-zaman önceliğiyle gerçekleşme

Motor, ITCH kaydından tarihsel defteri kurar. Simüle defter, aynı emirleri ve stratejinin kabul edilen pasif emirlerini fiyat-zaman sırasına alır. Stratejinin önceliği sanal borsaya varış anında belirlenir. Strateji simüle defteri görür. Grafik ve gerçekleşmemiş kâr/zarar için tarihsel defterin orta fiyatı kullanılır.

- Sürekli işlemde tarafı, fiyatı ve miktarı bilinen görünür gerçekleşme, simüle defterde yalnızca bir kez dağıtılır. Tarihsel satış bir alış fiyatına ulaşmışsa o fiyattan veya daha yüksek alışlardan başlayabilir. Alış işlemi için satış tarafında kural simetriktir. Aynı fiyatta önce sıradaki emir dolar.
- Bir 100 TL satışının 10 lotu bizim öndeki alışımıza giderse, arkadaki tarihsel alışın 10 lotu simüle defterde açık kalır. Sonraki işlemler bu bakiyeyi kullanabilir. ITCH iptali geldiğinde ilgili emrin simüle bakiyesi de kaldırılır. Örnekte tarihsel ITCH defteri kayıttaki 10 lotluk gerçekleşmeyi kendi emir kimliğine uygular.
- `E` mesajı tarihsel emrin kademesini, uygun `C` mesajı ayrıca yayımlanan işlem fiyatı ve bayrakları sağlar. Müzayede, gizli işlem ve yoruma açık mesajlar simüle emirler için hacim kaynağı olarak kullanılmaz. Eşleşemeyen lotlar ayrı sayılır.
- Tarihsel bir defter güncellemesi simüle alış ve satış kademelerini kesiştirirse motor bu olaydan gerçekleşme üretmez. Stratejinin açık emirlerini geri çeker. Kesişme sürüyorsa ve tarihsel defter geçerliyse, kesişmeye neden olan ve tarihsel defterde bulunmayan fiyat kademelerini kaldırır. Düzeltme sayısı panelde, kaldırılan lotlar eşleştirme günlüğünde görünür.

Bu model, gözlenen dış işlem akışını kullanarak emir önceliğinin etkisini ölçer. Piyasa katılımcılarının verdiğimiz emre nasıl tepki vereceği kayıt üzerinden gözlenemez. Portföy sanal kısa pozisyona izin verir ve ayarlı mutlak pozisyon sınırını uygular. Komisyon gerçekleşen kâr/zarara ve nakde yansır. [BISTECH ITCH şartnamesi](https://www.borsaistanbul.com/files/bistech-itch-protocol-specification.pdf)

İki taraflı pasif kotasyon ve replay yaklaşımı için FastMM ile HftBacktest incelendi. ASELS stratejisi, Pay Piyasası seans davranışı ve BISTECH ikili codec proje içinde uygulandı. [FastMM](https://github.com/ziyangliu-666/FastMM) · [HftBacktest gerçekleşme modeli](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)

### Kendi stratejini ekleme

1. [Alış derinliği örneğini](examples/asels_alis.py) proje kökünde `benim_stratejim.py` olarak kopyala. Strateji kimliğini ve `on_event` içindeki karar kurallarını değiştir. `get_strategies()`, stratejiyi oluşturan sınıfı ve dashboard’un göstereceği parametreleri tanımlar.
2. Proje kökünde `python -B -m dashboard --strategy-module benim_stratejim` çalıştır. Birden çok modül için `--strategy-module` seçeneğini tekrarla.
3. Dashboard’daki **Strateji** menüsünden kaydını seç, ayarları uygula ve replay’i başlat.

Hazır dosyayı kopyalamadan denemek için `python -B -m dashboard --strategy-module examples.asels_alis` çalıştır ve **Alış derinliği örneği** seç.

`on_event(event, view, updates)` çağrısı salt okunur piyasa ve emir bilgisi alır. Emir vermemek için `()`, emir vermek için örneğin `(Enter("B", view.best_bid, 10),)` döndür. `"B"` alış, `"S"` satış, fiyat ham tamsayı, miktar pay adedidir. Örneğin `price_decimals=2` için `12345`, 123,45 ₺ anlamına gelir. `Replace` ve `Cancel` kendi açık emirlerinin `order_id` değerini kullanır. Motor sürekli işlem fazında pasif Day limit emirleriyle ve her payda taraf başına bir açık emir ile bir bekleyen istekle çalışır. `view.symbol` ilgili payı gösterir. Çok kanallı koşuda `event.sequence` birleştirilmiş replay sırasıdır. Ham kaynak sırasına `event.source_sequence` üzerinden ulaşılır.

`--strategy-module` için yerel Linux Docker motoru, cgroup v2 ve önceden indirilmiş `python:3.12-slim` imajı gerekir. Windows’ta Linux konteyner motorunu kullan. Her pay için ayrı süreç çalışır. Kapsayıcı ağsızdır, yalnız seçili kaynakların salt okunur kopyasını görür ve CPU, bellek, süreç ile yanıt süresi sınırlarına tabidir. Çalıştırıcı bu sınırları doğrulayamazsa harici modülü yüklemeyi reddeder. Hazır stratejiler normal dashboard başlatmasıyla çalışır.

```powershell
docker pull python:3.12-slim
python -B -m dashboard --strategy-module examples.asels_alis
```

[API tipleri](dashboard/strategy_api.py) · [Kayıt ve parametre şeması](dashboard/strategy_registry.py) · [Docker çalışma seçenekleri](https://docs.docker.com/reference/cli/docker/container/run/)

## Replay

```powershell
.\build\hft_replay.exe .\capture.pcap 239.1.2.3 31000
```

IPv4 adresini ve UDP portunu kayıttaki **Pay Piyasası ITCH kanalının** hedefiyle değiştir. Karma kayıtlarda yalnızca seçilen hedefe ait paketler işlenir. Linux'ta çalıştırılabilir dosya `./build/hft_replay`. İlk sürüm klasik PCAP 2.4, Ethernet/VLAN ve IPv4/UDP destekler. Seçilen hedefte tek kaynak ve MoldUDP64 oturumu bekler. Kayıt `T` ve `S/O` seans başlangıcını içermeli. Bu komut tek kaydı denetler. Yedek akışla kurtarma dashboard’un kullandığı `hft_asels_feed` çıkarıcısında yer alır.

Çıktı UTF-8 ve sekmeyle ayrılmıştır. Zamanlar Unix epoch nanosaniye cinsindendir. Fiyatlar, defterin referans verisindeki fiyat ölçeğine karşılık gelen ham işaretli tamsayılardır.

| Satır | Alanlar |
|---|---|
| `REPLAY` | Son uygulanan Mold sıra numarası, o mesajı taşıyan paketin yakalama zamanı, son ITCH borsa zamanı, bitiş durumu |
| `BOOK` | Defter ID, sembol, fiyat ölçeği, round lot, ranking type, son defter durumu |
| `TICK` | Defter ID, alt fiyat, üst fiyat, adım |
| `ORDER` | Defter ID, taraf, fiyat, ranking time, ranking sequence, order ID, kalan görünür miktar |

`capture_prefix` seans başlangıcından dosya sonuna kadar doğrulanan kesiti gösterir. `itch_end` ve `mold_end` yakalanan kapanış işaretlerini belirtir. Sıra boşluğu veya hatalı girdi, standart hata akışına yazılır ve program sıfırdan farklı çıkış kodu verir. Görünür miktarı sıfır olan veya piyasa fiyatı işareti taşıyan emirler kimlikleriyle izlenir ve fiyatlı `ORDER` çıktısı dışında kalır.

Seans sonunda açık emir kalmayabilir. Seans içindeki bir anı görmek için seçilen akışın MoldUDP64 sıra numarasını ver. Örnekte defter 10.000.000 numaralı mesaj uygulandıktan sonra `sequence_limit` durumuyla yazılır.

```powershell
.\build\hft_replay.exe .\capture.pcap 239.1.2.3 31000 --through-sequence 10000000
```

## Gecikmeyi ölç

Aynı komutun sonuna `--metrics` ekle. Defter çıktısı standart çıktıdan, ölçüm satırı standart hata akışından gelir.

```powershell
.\build\hft_replay.exe .\capture.pcap 239.1.2.3 31000 --metrics
```

`replay_ns`, dosya başlığı okunduktan sonra kalan PCAP'ı okuma ve deftere uygulama süresidir. `snapshot_ns`, son defteri sıralama ve çıktıyı yazma süresidir. `packet_p99_ns`, örneklenen veri paketlerinin %99'unun MoldUDP64, ITCH ve defter işleme adımlarını tamamladığı süreyi gösterir. Paket ölçümü yerel işlem süresini kapsar. Ayrıntılı yöntem ve ölçüm sonuçları [BENCHMARK.md](BENCHMARK.md) dosyasında.

PCAP dosyan yoksa Python 3.9+ ile kendi test verisini üreten benchmark'ı çalıştırabilirsin.

```powershell
python -B .\tools\benchmark.py .\build\hft_replay.exe
```
