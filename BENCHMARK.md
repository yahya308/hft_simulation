# PCAP replay performans ölçümü

## Sentetik kayıt

AMD Ryzen 7 7840HS, Windows build 26200, MinGW GCC 16.2 ve CMake Release (`-O3 -DNDEBUG`). Program tek replay iş parçacığıyla çalıştı. Ölçümde aynı dosyayla her kip için önce bir ısınma koşusu, ardından beş koşu yapıldı.

Sentetik PCAP **4.016.523 ITCH mesajı**, **167.357 paket** ve **182.474.737 bayt** içeriyordu. Otuz iki defterde her tarafta 256 olmak üzere toplam **16.384 açık emir** tutuldu. Sabit tohumla seçilen defter ve emirler üzerinde ekleme, silme, kısmi/tam gerçekleşme ve aynı ID ile silip yeniden ekleme uygulandı.

| Ölçüm | Beş koşunun medyanı | En düşük ve en yüksek |
|---|---:|---:|
| Ölçüm kapalı, süreç başından sonuna | 1.768,66 ms | 1.750,13 – 1.812,32 ms |
| Ölçüm kapalı, ITCH mesajı/s | **2.270.937** | 2.216.234 – 2.294.991 |
| Ölçüm kapalı, PCAP paketi/s | 94.623 | 92.344 – 95.626 |
| Ölçüm kapalı, dosya hızı | 98,39 MiB/s | 96,02 – 99,43 MiB/s |
| Ölçüm açık, dosya okuma ve replay | 1.506,79 ms | 1.495,05 – 1.536,15 ms |
| Ölçüm açık, son defteri sıralama ve yazma | 145,44 ms | 144,38 – 161,50 ms |
| Veri paketi işleme p50 | **7,00 µs** | 6,90 – 7,00 µs |
| Veri paketi işleme p95 | 10,10 µs | 10,00 – 10,80 µs |
| Veri paketi işleme p99 | **14,00 µs** | 13,00 – 14,80 µs |
| Veri paketi işleme p99,9 | 51,60 µs | 37,90 – 64,00 µs |
| Veri paketi işleme, koşu maksimumu | 1.170,60 µs | 461,40 – 2.284,40 µs |

Ölçümden önce üç ayrı çalıştırmada defter çıktısı bayt düzeyinde aynıydı. Son Mold sıra numarası ve 16.384 açık emir doğrulandı. Windows çıktısının SHA-256 özeti `a9a1a9af6450b52c45770dcd3f9e28663bdc9d46b41701871f46f27d105ee812`.

## Yerel gerçek BIST kaydı

Aynı cihazda 27,9 GB'lık, birden çok multicast kanalını içeren gerçek PCAP'ın tamamı tarandı. Replay, seçilen tek Pay Piyasası kanalından **39.854.740 ITCH mesajı** ve **13.777.943 UDP paketi** işledi. Seans başlangıcı ve bitişi (`itch_end`) görüldü. Seçili kanalda sıra boşluğu veya defter uyuşmazlığı nedeniyle duruş olmadı.

| Ölçüm | Son doğrulama koşusu |
|---|---:|
| Dosya okuma ve replay | 50,974 s |
| Seçili kanalda etkin hız | yaklaşık 782.000 ITCH mesajı/s |
| Seans sonu defter çıktısı | 5,706 ms |
| Veri paketi işleme p50 | **0,70 µs** |
| Veri paketi işleme p95 | 2,70 µs |
| Veri paketi işleme p99 | **8,20 µs** |
| Veri paketi işleme p99,9 | 19,90 µs |
| Gözlenen en uzun paket işleme | 26,924 ms |

Etkin hız tüm dosyayı tarama süresine göre hesaplandı ve yalnızca seçili akışın mesajları sayıldı. Yüzdelikler 12.742.385 veri paketinden 65.536 örnekle hesaplandı. Paket süreleri, UDP yükü belleğe alındıktan sonraki yerel işlemi ölçer. Tekrar çalıştırmalarda seans sonu TSV çıktısı bayt düzeyinde aynıydı. `--through-sequence 10000000` ile alınan seans içi görüntüde **109.498 görünür fiyatlı emir** vardı. Ham PCAP ve defter çıktıları yerel çalışma alanında tutuldu.

## Yeniden çalıştırma

Önce [README](README.md) adımlarıyla Release derlemesi al. Python 3.9+ ile bu ölçüm için kullanılan iş yükünü çalıştır.

```powershell
python -B .\tools\benchmark.py .\build\hft_replay.exe --messages 4000000 --runs 5
```

Linux'ta çalıştırılabilir dosya yolu `./build/hft_replay`. Betik PCAP'ı geçici dizinde üretir ve işlem sonunda temizler. `--books`, `--orders-per-side` ve `--seed` ile iş yükü değiştirilebilir. `--messages` yalnız iş yükü bölümünü sayar. Tablodaki throughput, referans, zaman ve seans mesajları da dahil olmak üzere uygulanan tüm ITCH mesajlarını sayar.

## Ölçüm sınırları

- Dışarıdan ölçülen süre süreç başlangıcını, PCAP okuma ve çözmeyi, defter güncellemelerini ve son TSV çıktısının yazılmasını kapsar. Zamanlanan koşularda çıktı işletim sisteminin boş aygıtına yönlendirilir. PCAP üretimi ve hash kontrolü ölçüm dışındadır.
- `--metrics` replay döngüsünü ve son TSV yazımını ayrı ölçer. Paket süresi, UDP yükü bellekten alındıktan sonra MoldUDP64, ITCH ve defter güncellemesini kapsar. Bir pakette en çok 24 ITCH mesajı vardı. Yüzdelikler paket başınadır ve her koşuda yeni mesaj içeren 167.356 paketten 65.536 örnekle hesaplandı. Her koşunun maksimumu tüm veri paketleri üzerinden alındı.
- Sentetik kipler beşer koşuluk ardışık gruplar halinde çalıştı. Gerçek veri tablosu bir tam dosya koşusunu gösterir. İki girdideki paket karışımı, işlenen akış sayısı ve dosya hacmi farklıdır. OUCH gecikmesi, sunucu ve istemci entegrasyonu tamamlandığında ayrıca ölçülecek.

## ASELS dashboard replay

Yerel dashboard, aynı gerçek kaydın seçilen Pay Piyasası kanalından çıkarılan **1.249.321 olayı** Maks hızda yaklaşık **8 saniyede** oynattı. Süre, PCAP hazırlama tamamlandıktan sonra Başlat ile seans sonu arasındaki yerel dashboard koşusudur. Varsayılan strateji 7.607 OUCH isteği, 12.291 sıralı yanıt ve 4.682 simüle gerçekleşme bildirimi üretti.

Uçtan uca sentetik kayıtta 1×, 1.000× ve Maks koşuları aynı portföy değerlerini ve aynı OUCH mesaj günlüğünü verdi. Test ayrıca sürekli işleme kadar ilerlemeyi, duraklatmayı ve iki tarafta gerçekleşmeyi doğrular. [Dashboard testi](tests/dashboard_test.py) · [OUCH ve sıra testleri](tests/test_simulation.py)

### Strateji API geçişi

Hazırlanmış aynı 1.249.321 ASELS olayı Python replay motorunda bekleme olmadan çalıştırıldı. Ölçüm JSON olay okumasını, defteri, stratejiyi ve yerel emir motorunu kapsar. PCAP hazırlama, HTTP ve tarayıcı ölçüm dışında. Her sürüm için beş koşunun medyanı alındı.

| Motor | Medyan süre | Olay/s |
|---|---:|---:|
| Strateji API öncesi | 5,84 sn | 213.970 |
| Strateji API ile | 5,69 sn | 219.411 |

Ayrı bir doğrulama koşusunda tüm OUCH istek ve yanıt dizisinin SHA-256 özeti iki sürümde de `d3068129e13d0a76f297623080b9cc49a777fd15566b0194ffd9dd88816d6ad6` olarak ölçüldü. Sonuç 4.682 gerçekleşme, −66 pay ve toplam −1.631,25 ₺ oldu. [Strateji API testleri](tests/test_strategy_api.py) · [Strateji dashboard testleri](tests/strategy_dashboard_test.py)

### Fiyat-zaman önceliğiyle simüle defter

Aynı PCAP’tan Match ID içeren **1.249.321 ASELS olayı** çıkarıldı. Varsayılan çift taraflı kotasyonla iki kayıtlı koşu, tüm OUCH ve eşleştirme olaylarının aynı SHA-256 özetini verdi. Motor, strateji kapalı koşuda her bin olayda örneklenen tam simüle ve tarihsel emir kayıtlarını karşılaştırdı. Emirler ve bakiyeler eşleşti.

| Sonuç | Varsayılan kotasyon | Dashboard’da korunan ayarlar |
|---|---:|---:|
| Bir emir için hedef miktar | 10 lot | 100 lot |
| Sanal emir gecikmesi | 100 µs | 1 µs |
| Uygun tarihsel hacim | 27.600.428 lot | 27.600.428 lot |
| Stratejiye dağıtılan | 49.027 lot | 327.138 lot |
| Diğer emirlere dağıtılan | 27.551.401 lot | 27.273.290 lot |
| Dağıtılamayan | 0 lot | 0 lot |
| Kesişme nedeniyle defter düzeltmesi | 2.635 | 2.291 |
| Düzeltmede kaldırılan simüle bakiye | 32.348 lot | 219.393 lot |
| Toplam simüle kâr/zarar | −811,125 ₺ | +4.365,00 ₺ |

Varsayılan ayarlarla ölçülen iki motor koşusu yaklaşık 9,1 saniye sürdü ve her OUCH ile eşleştirme olayını özetleme maliyetini içerdi. Korunan ayarlar dashboard’da hazır olay önbelleğiyle Maks kipte 8,8 saniyede tamamlandı. Ham PCAP hazırlama süreye dahil değil. Düzeltmede kaldırılan bakiye işlem olarak kaydedilmiyor. [Simülasyon testleri](tests/test_simulation.py) · [Dashboard entegrasyon testi](tests/dashboard_test.py)

### Birden fazla pay

Aynı kaydın dört Pay Piyasası kanalından 611 pay listelendi. Varsayılan kotasyon ayarlarıyla ASELS.E (21001) ve THYAO.E (21004) birlikte, hazır önbellekten Maks kipte 2.189.431 olay işledi. Gözlenen bir dashboard koşusu 19,0 saniye sürdü. THYAO.E tek başına çalıştırıldığında 940.110 olay işlendi ve çoklu koşudaki pozisyon, gerçekleşme sayısı ve kâr/zarar aynen elde edildi. [Çoklu motor testleri](tests/test_multi_simulation.py)
