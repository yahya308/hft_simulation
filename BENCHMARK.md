# PCAP replay performans ölçümü

## Sonuçlar

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

## Yeniden çalıştırma

Önce [README](README.md) adımlarıyla Release derlemesi al. Python 3.9+ ile bu ölçüm için kullanılan iş yükünü çalıştır.

```powershell
python -B .\tools\benchmark.py .\build\hft_replay.exe --messages 4000000 --runs 5
```

Linux'ta çalıştırılabilir dosya yolu `./build/hft_replay`. Betik PCAP'ı geçici dizinde üretir ve işlem sonunda temizler. `--books`, `--orders-per-side` ve `--seed` ile iş yükü değiştirilebilir. `--messages` yalnız iş yükü bölümünü sayar. Tablodaki throughput, referans, zaman ve seans mesajları da dahil olmak üzere uygulanan tüm ITCH mesajlarını sayar.

## Ölçüm sınırları

- Dışarıdan ölçülen süre süreç başlangıcını, PCAP okuma ve çözmeyi, defter güncellemelerini ve son TSV çıktısının yazılmasını kapsar. Zamanlanan koşularda çıktı işletim sisteminin boş aygıtına yönlendirilir. PCAP üretimi ve hash kontrolü ölçüm dışındadır.
- `--metrics` replay döngüsünü ve son TSV yazımını ayrı ölçer. Paket süresi, UDP yükü bellekten alındıktan sonra MoldUDP64, ITCH ve defter güncellemesini kapsar. Bir pakette en çok 24 ITCH mesajı vardı. Yüzdelikler paket başınadır ve her koşuda yeni mesaj içeren 167.356 paketten 65.536 örnekle hesaplandı. Her koşunun maksimumu tüm veri paketleri üzerinden alındı.
- Kipler beşer koşuluk ardışık gruplar halinde çalıştı. Sonuçlar bu cihazın ve sentetik girdinin performans tabanıdır. Gerçek BIST trafiği üzerinde doğrulama, yakalama biçimi, paket yoğunluğu ve seans koşullarıyla tekrarlanmalıdır. OUCH gecikmesi, sunucu ve istemci entegrasyonu tamamlandığında ayrıca ölçülecek.
