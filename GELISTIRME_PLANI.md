# BISTECH HFT simülatörü geliştirme planı

## Amaç ve ilk kapsam

Proje, Borsa İstanbul ITCH trafiğini bir yakalama dosyasından okuyarak görünür emir defterini emir bazında yeniden kuracak. Yerel bir BISTECH OUCH sunucusu strateji emirlerini kabul edecek, sanal borsa saatinde sıraya alacak ve gerçekleşme sonuçlarını üretecek. Aynı girdi dosyaları ve ayarlarla aynı defter durumu ve aynı yanıt dizisi elde edilmeli.

İlk hedef Pay Piyasası, tek mantıksal ITCH akışı, seçilmiş paylar ve sürekli işlem. OUCH kapsamı yuvarlak lotlu Day limit alış ve satış emirleri ile Enter, Replace ve Cancel. Açılış, kapanış, devre kesici ve diğer fazlar tarihsel defterde izlenir. Simüle eşleştirme sürekli işlem fazında çalışır. Çoklu partition, ayrı eski ITCH profili, IOC/FOK, Mass Quote, açığa satış, rezerv emir ve müzayede eşleştirmesi sonraki kapsamlar. Parçaların uygunluğu BISTECH revizyonu ve kullanılan kayıt üzerinden doğrulanır. [BIST teknik dokümanları](https://borsaistanbul.com/en/technical-resources/technical-documents) · [Pay Piyasası İşleyişi](https://www.borsaistanbul.com/piyasalar/pay-piyasasi/piyasa-isleyisi)

## Veri akışı ve modül sınırları

```text
PCAP/PCAPNG → Ethernet/IP/UDP → MoldUDP64 → BISTECH ITCH → tarihsel emir defteri
                                   ↓                        ↓
                              sıra doğrulama           piyasa olayları
                                                            ↓
TCP → SoupBinTCP → BISTECH OUCH → sanal saat → simüle emirler ve gerçekleşmeler
                                              ↓
                                     OUCH yanıtları ve olay günlüğü
```

| Modül | Sorumluluk |
|---|---|
| Yakalama okuyucusu | Girdi biçimini, link tipini, yakalama zamanını ve paket uzunluğunu doğrula. Ağ katmanından alınan UDP payload ve akış kimliğini taşıma katmanına ver. |
| MoldUDP64 | Oturum başlığı, mesaj sayısı, uzunluk önekleri, heartbeat ve oturum sonunu ayrıştır. Mesaj bazında sıra sürekliliğini doğrula. |
| BISTECH ITCH | Seçilen revizyondaki tür, uzunluk, signed fiyat ve big endian alanları çöz. Dosya ofseti veya paket numarası ve Mold sıra numarası hata mesajına taşınır. |
| Tarihsel defter | Emir kimliğini kitap ve taraf kapsamında izle. Sıralama, fiyat seviyesi, miktar, işlem fazı ve referans verisini koru. |
| Sanal saat | Veri görünürlüğü, emir varışı ve yanıt teslimi için açık zaman kuralları uygula. Sabit girdiyle aynı olay sırasını üret. |
| OUCH sunucusu | TCP ve SoupBinTCP çerçeveleme, oturum, heartbeat, sıralı giden mesajlar ve yeniden bağlantı. Uygulama komutlarını sanal saate teslim et. |
| Gerçekleşme modeli | Görünür kuyruğu, simüle emirleri, kısmi gerçekleşmeleri ve paylaşılan işlem hacmini izle. Bir fill'in nedenini ve bağlı olduğu piyasa olayını kaydet. |

Çekirdek C++20 ve CMake ile kurulur. Başlangıçta tek yazıcı kullanılır. Emirlerin ve fiyatların kimlikleri sabit genişlikli tamsayılarla taşınır. Sıralı sonuç çıktısı için hash tablosu iterasyon sırasına bağımlı kalınmaz. Yakalama okuyucusu bağımsız bir sınır olarak tutulur, böylece PCAPNG desteği veya farklı ağ adaptörleri aynı ITCH ve defter koduyla çalışabilir.

## Yeniden kullanım kararları

| Aday | Kullanım |
|---|---|
| PcapPlusPlus ve libpcap | Dosya ve ağ paketi okuma için ilk adaylar. PcapPlusPlus Unlicense, libpcap BSD-3-Clause. Platform ve gerçek kayıtla derleme, link tipi ve zaman hassasiyeti doğrulanır. [PcapPlusPlus](https://github.com/seladb/PcapPlusPlus) · [libpcap](https://github.com/the-tcpdump-group/libpcap) |
| itchcpp | MIT. `MoldUdp64Decoder` ve sıra izleme yaklaşımı incelenir. Taşıma ile Nasdaq sözlüğü arasındaki bağı ayırmak, BIST'e özgü oturum ve recovery davranışını test etmek gerekir. [itchcpp taşıma katmanı](https://github.com/bbalouki/itchcpp#feed-ingestion-transport) |
| FastMM | MIT. Genel MoldUDP64 `parse_packet` işlevi ilk çekirdeğe uyarlandı. BIST ITCH çözümü ve sıra durumu yerel. Kaynak ve lisans kaydı `THIRD_PARTY_NOTICES.md` içinde. [FastMM çerçeveleme kaynağı](https://github.com/ziyangliu-666/FastMM/blob/main/src/codecs/moldudp/moldudp64.cpp) |
| Wireshark/TShark 4.6+ ve OMI | Wireshark'ın yerleşik BIST ayrıştırıcılarıyla seçilmiş alanları karşılaştır. OMI'nin küçük PCAP'lerini biçim testleri için değerlendir. ITCH fiyatlarının doğru ölçeği ve mesaj revizyonu resmi belgelerle doğrulanır. [BIST ITCH](https://www.wireshark.org/docs/dfref/b/bist_itch.html) · [BIST OUCH](https://www.wireshark.org/docs/dfref/b/bist_ouch.html) · [OMI BIST PCAP](https://github.com/Open-Markets-Initiative/omi-data-packets/blob/main/Bist/ReadMe.md) |
| bist-orderbook-analysis ve ITCH-BIST | Python projesi ITCH 2112 üzerinde görünür fiyat seviyelerini karşılaştırma adayı. Yayınlanan tam kayıt raporu sıra boşlukları içeriyor. Proje metadata'sında MIT beyanı var. ITCH-BIST, Apache-2.0 lisanslı eski C++ `.VRD` uygulaması. [Python projesi](https://github.com/prettybeaches/bist-orderbook-analysis) · [Tam kayıt raporu](https://github.com/prettybeaches/bist-orderbook-analysis/blob/main/docs/full-capture-validation.md) · [ITCH-BIST](https://github.com/metehaniset/ITCH-BIST) |
| nasdaq-matching-engine ve CppTrader | MIT. Nasdaq simülatöründe SoupBinTCP oturumu, protokol testleri ve iki eşleştiricinin diferansiyel testi incelenir. CppTrader sentetik çekirdek adayı. BIST codec ve BIST kuralları resmi profilden uygulanır. [Nasdaq simülatörü](https://github.com/yazanqwasmi/nasdaq-matching-engine) · [CppTrader](https://github.com/chronoxor/CppTrader) |
| HftBacktest | MIT. Gecikme, kuyruk ve fill modelleri için referans. [Fill modelleri](https://hftbacktest.readthedocs.io/en/latest/order_fill.html) |

Bir bileşen entegre edildiğinde sürümü veya commit'i sabitle, kullanılan dosyaları ve kaynak adresini kaydet, lisans bildirimlerini koru. Gerçek piyasa dosyalarının kullanım ve paylaşım koşulları kod lisanslarından bağımsız olarak teyit edilir. [BIST DataStore](https://www.borsaistanbul.com/veriler/gecmise-donuk-veri-satisi)

## Doğruluk kuralları

### Tarihsel akış

- İlk profil ITCH v2112. SR 3.12 öncesindeki `Order Book Position` ile SR 3.12 sonrasındaki `Ranking Time` ve `Ranking Sequence Number` ayrı şema profilleridir. Profil, dosyanın üretim ortamı ve seans tarihiyle eşlenir. [BISTECH ITCH §4 ve Ek A](https://www.borsaistanbul.com/files/bistech-itch-protocol-specification.pdf) · [SR 3.12 duyurusu](https://www.borsaistanbul.com/files/duyuru-32248-EN.pdf)
- Mold sıra numarası paketin ilk mesajına aittir. Çok mesajlı datagramda sonraki mesajlar art arda numaralanır. Çoğaltılmış mesajlar tekrar uygulanmaz. Boşlukları yakalanmış yedek hat veya rewinder verisiyle doldur. GLIMPSE bulunduğunda snapshot'ı yayınladığı sıradan itibaren besle ve önceki eksik aralığı kayıp olarak raporla. İlk geliştirme dilimi çözülemeyen boşlukta durur. [MoldUDP64](https://borsaistanbul.com/files/moldudp64-protocol-specification9890234EF41CE5A6B1C8A4A3.pdf) · [ITCH Ek E](https://www.borsaistanbul.com/files/bistech-itch-protocol-specification.pdf#page=32)
- Emir anahtarı `(order_book_id, side, order_id)`. Fiyat önceliği alışta daha yüksek, satışta daha düşük fiyata gider. Aynı fiyatta küçük `Ranking Time`, eşitlikte küçük `Ranking Sequence Number` önce gelir. Miktarı ve fiyat ölçeğini tamsayı olarak koru. [ITCH Ek A](https://www.borsaistanbul.com/files/bistech-itch-protocol-specification.pdf)
- `R/L` enstrüman tanımı ve fiyat adımı, `T/S/O` zaman ve durum, `A/E/C/D/Y` görünür defter yaşam döngüsü için işlenir. `E/C` açık miktarı düşürür, miktar sıfıra indiğinde emir kalkar. `P` ayrı işlem kaydıdır. Tarihsel replace, aynı ID ile `D+A` olarak gelir. `Y` yalnız referans verdiği görünür defteri temizler. [BISTECH ITCH §4](https://www.borsaistanbul.com/files/bistech-itch-protocol-specification.pdf)

### Sanal saat ve OUCH

- Yakalama zamanı, `T` ve mesaj nanosaniyesinden çıkan borsa zamanı, sıralama zamanı ve simüle emir varış zamanı ayrı tutulur. Çekirdek her ITCH akışında Mold sırasını korur. Farklı akışlar eklendiğinde birleştirme kuralı ayrıca tanımlanır.
- Aynı OUCH bağlantısında geliş sırası korunur. İlk sanal saat kuralı eşit zamandaki tarihsel olayları önce uygular, ardından geçerli emir varışlarını kayıtlı giriş sırasıyla işler. İstemcinin gördüğü piyasa bilgisi, o anda teslim edilmiş olaylarla sınırlanır. Sabit feed ve emir gecikmeleriyle başla, daha sonra doğrulanmış gecikme örneklerini destekle.
- Mantıksal hesap, işlem günü ve token emir kimliği için kullanılır. İstemcinin resend'i tek bir emir etkisi üretir. Accepted mesajı o emrin sonraki lifecycle yanıtlarından önce gider. SoupBinTCP sıra numarası ve yeniden bağlanma durumu hesap/oturum bağlamında tutulur. [BISTECH OUCH §3–4](https://borsaistanbul.com/files/bistech-ouch-protocol-specification.pdf) · [SoupBinTCP](https://borsaistanbul.com/files/soupbintcp-protocol-specificationB250FDF1BDD4.pdf)
- Limit emirlerde fiyat değişikliği ve miktar artışı zaman önceliğini düşürür. Mevcut fiyat ve daha düşük miktar için öncelik koruma testi yazılır. Replace miktarında doküman tablosu ile §4.1.2.1 örneklerinin ifadesi farklı. Örnek, hedef toplamı açık miktar ve kümülatif gerçekleşme toplamı olarak veriyor. Bu davranışı seçilen OUCH profiline ait kısmi gerçekleşme testiyle sabitle. [BISTECH OUCH §4.1.2](https://borsaistanbul.com/files/bistech-ouch-protocol-specification.pdf) · [Pay Piyasası Prosedürü](https://borsaistanbul.com/files/pay-piyasasi-proseduru.pdf)

### Gerçekleşme modeli

Tarihsel defter, kamuya yayımlanan olayları uygular. Simüle emirler kendi yaşam döngüsünü tutar. Pasif emir geldiğinde önündeki emirleri fiyat ve sıra açısından belirle. Takip edilen iptal veya replace işlemi öndeki görünür miktarı değiştirebilir. Gerçekleşme yalnız uygun tarihsel işlem hacminden, her hacim dilimi en fazla bir kez kullanılarak üretilir. IOC/FOK veya likidite alan replay davranışı eklenirse tüketim ve sonraki tarihsel olaylarla uyum açıkça modellenir. Sentetik kipte iki emir tarafı da simülatöre girer ve eşleştirici işlem, kalan miktar ve ITCH etkisini üretir. Tarihsel akışta görünmeyen likidite ve piyasadaki katılımcıların varsayımsal tepkisi, fill sonuçlarının belirsizliğidir. [ITCH §3](https://www.borsaistanbul.com/files/bistech-itch-protocol-specification.pdf) · [HftBacktest fill modelleri](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)

## Teslimatlar ve kabul ölçütleri

| Aşama | Teslimat | Kabul ölçütü |
|---|---|---|
| 1. Çalışan replay | CMake hedefleri, basit CLI, seçili UDP akışı, MoldUDP64, ITCH v2112 ve görünür defter | Sentetik PCAP uçtan uca okunur. Aynı fiyattaki emir sırası, kısmi gerçekleşme, iptal, flush ve tekrar paket kontrol edilir. Çözülemeyen sıra boşluğu konum ve beklenen sıra ile raporlanır. |
| 2. Gerçek veri doğrulaması | Kullanım izni teyit edilmiş kesintisiz PCAP ve paket/mesaj/defter karşılaştırması | TShark ile seçili alanlar eşleşir. Belirli sıra numaralarında en iyi alış/satış, miktar ve tam sıra karşılaştırılır. Eksik veya profil dışı kayıtların davranışı yeniden üretilebilir. |
| 3. OUCH ve sanal saat | Yerel SoupBinTCP, BIST OUCH codec, oturum, hesap/token, Enter/Replace/Cancel | Resmi mesaj örnekleriyle byte karşılaştırması. Bölünmüş TCP verisi, tekrar gönderim, reconnect ve kısmi gerçekleşme sonrası replace testleri. [BIST OUCH test senaryoları](https://www.borsaistanbul.com/files/equity-market-ouch-certification-program.pdf) |
| 4. Gerçekleşme | Kuyruk modeli ve sentetik fiyat-zaman eşleştiricisi | Farklı fiyatlar, aynı fiyat, kısmi fill, önceliği koruyan ve düşüren replace, seans durumu ve kayıp veri senaryoları. Aynı girdilerle aynı yanıtlar ve defter özeti. |
| 5. Dayanıklılık ve performans | Recovery, kontrol noktası, tam seans ölçümü, hata enjeksiyonu | Temsilî veri üzerinde throughput, p50/p99/p99.9 ve bellek tabanı. Decode, defter ve OUCH uçtan uca süreleri ayrı ölçülür. Sıcak yol optimizasyonları defter/yanıt eşitliği testiyle karşılaştırılır. |

İlk dilim hazır olduğunda, gerçek veriye erişimi beklerken eksik ve yeniden sıralanmış paketler sentetik fixture ile sınanabilir. Tam seans throughput hedefi, yakalama profili ve hedef donanım görüldüğünde belirlenir. Ölçüm raporu derleyici, optimizasyon seviyesi, çekirdek ve iş yükünü içerir.

## İlk uygulamanın durumu

- `hft_replay` klasik PCAP'tan tek hedef IPv4/UDP akışını okuyor. FastMM'den uyarlanan MoldUDP64 çerçeveleme, ITCH v2112 decoder'ı ve defter bir CMake hedefinde çalışıyor. Görünür fiyatlı emirler UTF-8 TSV olarak sıralanıyor. [Derleme ve kullanım](README.md)
- Yerel ortamda PcapPlusPlus kurulu olmadığı için ilk okuyucu bağımlılıksız. Klasik PCAP 2.4, Ethernet/VLAN ve IPv4/UDP ile sınırlandı. PCAPNG, çoklu oturum, gecikmeli paketleri yeniden sıralama ve recovery sonraki geliştirme diliminde değerlendirilecek.
- Tarihsel defter, ilk doğrulama için `std::map` ve çıktıda sıralama kullanıyor. Seans başı `T` ve `S/O` ile doğrulanıyor. Kayıt bir seans önekiyse çıktı bunu açıkça gösteriyor. Bu dilimde `P/M/V/Z` türleri tanınıp uzunlukları doğrulanıyor. Tam işlem ve referans olayları, simüle fill modeline geçerken eklenecek.
- Windows üzerinde MinGW Release derlemesi ve sentetik PCAP entegrasyon testleri geçti. `--metrics` ile paket işleme, dosya replay'i ve defter çıktısı süreleri ölçülebiliyor. Sentetik performans tabanına ek olarak gerçek BIST PCAP'ta bir Pay Piyasası kanalı seans sonuna kadar işlendi ve tekrarlar aynı defter çıktısını verdi. `--through-sequence` seans içindeki bir olaydan sonra defteri yazabiliyor. TShark ile bağımsız alan karşılaştırması ikinci aşamada, OUCH sunucusu ve sanal zamanlayıcı üçüncü aşamada. [Benchmark](BENCHMARK.md)
