# BISTECH HFT simülatörü

`hft_replay`, BISTECH ITCH v2112 taşıyan bir PCAP'tan emir defterini yeniden kurar ve görünür fiyatlı emirleri sıralı olarak gösterir. [Geliştirme planı](GELISTIRME_PLANI.md)

## Nasıl çalışır?

1. PCAP dosyasında verdiğin IP ve UDP portuna giden paketleri bulur.
2. MoldUDP64 sıra numaralarını kontrol eder ve ITCH mesajlarını okur. Eksik sıra numarası görürse durur.
3. Emir ekleme, gerçekleşme ve silme mesajlarını deftere uygular. Dosyanın sonunda kalan fiyatlı emirleri yazar. Aynı fiyattaki emirlerde BIST'in sıralama zamanı ve sıra numarası kullanılır.

Bu sürüm çevrimdışı piyasa verisi replay'i yapar. OUCH emir girişi geliştirme planının sonraki aşamasında.

## Derleme ve test

CMake 3.20+, Ninja ve C++20 derleyicisiyle derle. Python 3, sentetik PCAP entegrasyon testlerini çalıştırır.

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

## Replay

```powershell
.\build\hft_replay.exe .\capture.pcap 239.1.2.3 31000
```

IPv4 adresini ve UDP portunu kayıttaki ITCH hedefiyle değiştir. Linux'ta çalıştırılabilir dosya `./build/hft_replay`. İlk sürüm klasik PCAP 2.4, Ethernet/VLAN ve IPv4/UDP destekler. Seçilen hedefte tek kaynak ve MoldUDP64 oturumu bekler. Kayıt `T` ve `S/O` seans başlangıcını içermeli. Seans içinden başlama ve sıra boşluğu kurtarma planın sonraki aşamasında.

Çıktı UTF-8 ve sekmeyle ayrılmıştır. Zamanlar Unix epoch nanosaniye cinsindendir. Fiyatlar, defterin referans verisindeki fiyat ölçeğine karşılık gelen ham işaretli tamsayılardır.

| Satır | Alanlar |
|---|---|
| `REPLAY` | Son uygulanan Mold sıra numarası, o mesajı taşıyan paketin yakalama zamanı, son ITCH borsa zamanı, bitiş durumu |
| `BOOK` | Defter ID, sembol, fiyat ölçeği, round lot, ranking type, son defter durumu |
| `TICK` | Defter ID, alt fiyat, üst fiyat, adım |
| `ORDER` | Defter ID, taraf, fiyat, ranking time, ranking sequence, order ID, kalan görünür miktar |

`capture_prefix` seans başlangıcından dosya sonuna kadar doğrulanan kesiti gösterir. `itch_end` ve `mold_end` yakalanan kapanış işaretlerini belirtir. Sıra boşluğu veya hatalı girdi, standart hata akışına yazılır ve program sıfırdan farklı çıkış kodu verir. Görünür miktarı sıfır olan veya piyasa fiyatı işareti taşıyan emirler kimlikleriyle izlenir ve fiyatlı `ORDER` çıktısı dışında kalır.

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
