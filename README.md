# BISTECH Pay Piyasası HFT simülatörü

Proje Borsa İstanbul Pay Piyasası için çevrimdışı replay ve tekli veya çoklu pay üzerinde kotasyon simülasyonu sunar. Dashboard PCAP, replay, strateji ve sonuçları aynı ekrandan yönetir. `hft_replay` kanal seviyesinde denetim ve TSV çıktısı için komut satırı aracı olarak kullanılabilir. [Geliştirme planı](GELISTIRME_PLANI.md)

## Nasıl çalışır?

1. PCAP dosyasında verdiğin IP ve UDP portuna giden paketleri bulur.
2. MoldUDP64 sıra numaralarını kontrol eder ve ITCH mesajlarını okur. Eksik sıra numarası görürse durur.
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
4. **Strateji** bölümünden **Çift taraflı kotasyon** veya **Tek taraflı alış** seç. Stratejinin ayarları ve ortak simülasyon ayarları aynı ekrandan uygulanır. Oynatma başladıktan sonra strateji veya ayar değiştirmek için **Başa al** düğmesini kullan. Stratejiyi açıp kapatma ve hız kontrolü replay sırasında da kullanılabilir. Ekrandaki **i** düğmeleri alanları açıklar.
5. **Oturumdaki hisseler** tablosu her hissenin pozisyonunu, kâr/zararını ve gerçekleşmelerini gösterir. Hisse adına tıklayarak defteri, grafiği ve emir günlüğünü o hisseye odaklayabilirsin. Odak değişirken diğer hisseler aynı replay’de işlemeye devam eder. **Emir defteri** bölümünde **Simüle** veya **Tarihsel ITCH** görünümü seçilebilir.

Her kanalın MoldUDP64 sırası ayrı doğrulanır. Birden fazla kanal seçildiğinde olaylar borsa zamanı ile birleştirilir. Eşit zaman damgasında önce UDP portu, sonra kaynak Mold sırası kullanılır. Strateji ayarları ortak uygulanır. Pozisyon limiti ve emirler hisse bazındadır.

### Test stratejisi ve OUCH

Strateji, seçili payların sürekli işlem fazında en iyi alış ve satış kademelerine iki pasif Day limit emir verir. Fiyatlar değiştiğinde kotasyonları OUCH `Replace` ile günceller. Yerel motor ikili SoupBinTCP veri çerçeveleri içinde `Enter`, `Replace`, `Cancel` isteklerini ve kabul, ret, iptal ve gerçekleşme yanıtlarını işler. Emir günlüğündeki mesaj bilgisinden token, istek/yanıt sırası ve ikili çerçeve görülebilir. Aynı sanal zaman damgasındaki tarihsel ITCH olayı, simüle emir varışından önce işlenir. Ağ üzerinden OUCH oturumu açma ve Mass Quote sonraki protokol kapsamıdır. [BISTECH OUCH](https://borsaistanbul.com/files/bistech-ouch-protocol-specification-sr-3-12.pdf)

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

`--strategy-module` ile yüklenen modül dashboard’u başlatan kullanıcının yerel yetkileriyle çalışır. Bu seçenek güvendiğin Python modülleri içindir. [API tipleri](dashboard/strategy_api.py) · [Kayıt ve parametre şeması](dashboard/strategy_registry.py)

## Replay

```powershell
.\build\hft_replay.exe .\capture.pcap 239.1.2.3 31000
```

IPv4 adresini ve UDP portunu kayıttaki **Pay Piyasası ITCH kanalının** hedefiyle değiştir. Karma kayıtlarda yalnızca seçilen hedefe ait paketler işlenir. Linux'ta çalıştırılabilir dosya `./build/hft_replay`. İlk sürüm klasik PCAP 2.4, Ethernet/VLAN ve IPv4/UDP destekler. Seçilen hedefte tek kaynak ve MoldUDP64 oturumu bekler. Kayıt `T` ve `S/O` seans başlangıcını içermeli. Seans içinden başlama ve sıra boşluğu kurtarma planın sonraki aşamasında.

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
