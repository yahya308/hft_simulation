# HFT simülatöründe kısmi türev ve zincir kuralı

## 1. Modelin cevapladığı soru

Replay’de strateji birden fazla hissede aynı anda pozisyon taşıyabiliyor. Bu sırada şu soruyu sormak istiyoruz. **Şu anki pozisyonlarımızı korursak, hisselerin referans fiyatları örneğin %1 düşerse toplam kâr/zararımız kaç TL değişir?**

Bu sorunun cevabı dashboard’daki **Portföy fiyat şoku** panelinde gösteriliyor. Panel, portföyün mevcut kâr/zararını, 1 yüzde puanı başına duyarlılığını, girilen şokun etkisini ve şok sonrası kâr/zararını veriyor. **Hisse bazında duyarlılık** tablosunda hangi hissenin toplam etkiye ne kadar katkıda bulunduğunu görebiliyoruz.

Hesap, replay’in belirli bir anındaki nakdi ve pozisyonları sabit tutar. Yalnızca değerlemede kullanılan referans fiyatları değiştirir. Böylece eşleştirme motorunun emirleri ve gerçekleşmeleriyle portföyün fiyat riskini aynı ekranda inceleyebiliyoruz. [Dashboard paneli](dashboard/static/index.html#L87)

## 2. Önce tek hisseyle sezgiyi kur

Bir hisseden **10 payı 100 ₺’den** aldığımızı ve komisyonun sıfır olduğunu varsayalım. Alıştan sonra nakit etkisi **−1.000 ₺**. Hisse fiyatı $m$ ise portföyün net değeri:

$$
\Pi(m)=-1\,000+10m.
$$

| Hisse fiyatı | Nakit | Pozisyonun değeri | Toplam kâr/zarar |
|---:|---:|---:|---:|
| 100 ₺ | −1.000 ₺ | 1.000 ₺ | 0 ₺ |
| 101 ₺ | −1.000 ₺ | 1.010 ₺ | +10 ₺ |
| 99 ₺ | −1.000 ₺ | 990 ₺ | −10 ₺ |

Fiyat **1 ₺ arttığında** kâr/zarar **10 ₺ artıyor**. Çünkü elimizde 10 pay var ve her payın değeri 1 ₺ artıyor. Fiyata göre türev bu **10** sayısı.

Kısa pozisyonda işaret ters. Örneğin pozisyon **−5 pay** ise fiyat 1 ₺ arttığında kâr/zarar 5 ₺ azalır. Fiyat 1 ₺ düştüğünde kâr/zarar 5 ₺ artar. Bunun nedeni, kısa pozisyonun kapatılması için gerekli payların fiyat yükseldiğinde daha pahalı hâle gelmesidir.

## 3. Birden fazla hisse varken neden kısmi türev?

Bir replay anında $n$ hisse için şu değerleri kullanıyoruz.

| Sembol | Anlamı | Birim |
|---|---|---|
| $Q_i$ | $i$ numaralı hissenin net pozisyonu. Uzun pozisyon pozitif, kısa pozisyon negatif, açık pozisyon yoksa sıfır. | Pay |
| $C_i$ | O hisseye ait simüle nakit bakiyesi. Alışlar nakdi azaltır, satışlar artırır, ücretler nakitten düşer. | ₺ |
| $m_i$ | Hissenin tarihsel ITCH defterinde son geçerli olan en iyi alış ve satışın ortalaması. | ₺/pay |
| $\mathbf m$ | Seçili hisselerin referans fiyatları vektörü. | ₺/pay |
| $\Pi$ | Sıfır başlangıç nakdi ve pozisyonuyla çalışan simülasyondaki toplam portföy kâr/zararı. | ₺ |

Hisse başına nakit ile açık pozisyonun değerini topladığımızda:

$$
\Pi(\mathbf m)
=\sum_{i=1}^{n}(C_i+Q_i m_i).
$$

Portföyün değeri birden fazla fiyata bağlı. **Sadece $j$ hissesinin fiyatını değiştirip diğer fiyatları sabit tuttuğumuzda** $m_j$’ye göre kısmi türevi hesaplarız. O hisseye $h$ TL fiyat değişimi verirsek, toplamın içinde sadece $Q_j m_j$ terimi değişir.

$$
\begin{aligned}
\frac{\partial \Pi}{\partial m_j}
&=\lim_{h\to0}
\frac{\Pi(m_1,\ldots,m_j+h,\ldots,m_n)-\Pi(\mathbf m)}{h}\\
&=\lim_{h\to0}\frac{Q_j(m_j+h)-Q_jm_j}{h}\\
&=\lim_{h\to0}\frac{Q_jh}{h}=Q_j.
\end{aligned}
$$

**Sonuç.** Bir hissenin fiyatı 1 ₺ arttığında portföy kâr/zararındaki değişim, o hissenin işaretli pozisyonuna sayısal olarak eşittir. Bu türevin paneldeki karşılığı **1 TL fiyat artışının etkisi**.

### Gradyan

Bütün hisselere ait kısmi türevleri aynı sırayla yazarsak gradyanı elde ederiz:

$$
\nabla_{\mathbf m}\Pi=(Q_1,Q_2,\ldots,Q_n)^{\mathsf T}.
$$

Örneğin ASELS pozisyonu −15, THYAO pozisyonu +100 ise, fiyat sırası ASELS ve THYAO olmak üzere gradyan $(-15,\ 100)^{\mathsf T}$ olur. ASELS’te 1 ₺ artış 15 ₺ kayıp, THYAO’da 1 ₺ artış 100 ₺ kazanç etkisi verir. Birden fazla fiyat aynı anda değişirse her etki kendi fiyat değişimiyle çarpılıp toplanır:

$$
\Delta\Pi=(\nabla_{\mathbf m}\Pi)^{\mathsf T}\Delta\mathbf m
=\sum_i Q_i\,\Delta m_i.
$$

## 4. Zincir kuralı tam olarak hangi adımda?

Dashboard’a bir TL tutarı yerine **ortak bir yüzde değişimi** giriyoruz. $s=-1$ tüm referans fiyatların %1 düşmesi, $s=2$ %2 artması demek. Yüzdeyi fiyatla çarparken $s/100$ kullanıyoruz. $m_i$ şok öncesi fiyat olmak üzere:

$$
m_i(s)=m_i\left(1+\frac{s}{100}\right),
\qquad
\frac{dm_i(s)}{ds}=\frac{m_i}{100}.
$$

Burada **yüzde girdisi fiyatı değiştiriyor, fiyat değişimi de kâr/zararı değiştiriyor**. İki değişim arasındaki bağlantıyı zincir kuralıyla kuruyoruz. $\Phi(s)=\Pi(\mathbf m(s))$ şok uygulanmış portföy kâr/zararı olsun.

$$
\begin{aligned}
\frac{d\Phi}{ds}
&=\sum_i
\underbrace{\frac{\partial\Pi}{\partial m_i}}_{\text{1 TL fiyat artışının etkisi}}
\underbrace{\frac{dm_i(s)}{ds}}_{\text{1 yüzde puanının fiyat etkisi}}\\
&=\sum_i Q_i \frac{m_i}{100}.
\end{aligned}
$$

Dolayısıyla **1 yüzde puanı başına** hisse etkisi $Q_i m_i/100$ ₺, portföy etkisi de bu değerlerin toplamıdır. Burada 1 yüzde puanı, örneğin şok girişinin 0’dan +1’e veya −2’den −1’e çıkmasıdır. Aynı yüzde şoku her hissede aynı TL fiyat farkını üretmez. **100 ₺’lik hissede %1 değişim 1 ₺, 400 ₺’lik hissede %1 değişim 4 ₺** eder.

Panelin hesapladığı değerler şöyledir:

$$
\boxed{
\begin{aligned}
\Delta\Pi
&=s\sum_i\frac{Q_i m_i}{100}\\
\Pi_{\text{şok sonrası}}
&=\Pi_{\text{mevcut}}+\Delta\Pi.
\end{aligned}}
$$

### Sonuç neden bu senaryo için tam?

Pozisyon $Q_i$ ve şok öncesi fiyat $m_i$ sabitken $d\Phi/ds$ de sabittir. Yeni fiyatları doğrudan portföy formülüne koyup eski değeri çıkarırsak aynı hesabı elde ederiz:

$$
\begin{aligned}
\Phi(s)-\Phi(0)
&=\sum_i\left[
C_i+Q_i m_i\left(1+\frac{s}{100}\right)
-(C_i+Q_i m_i)\right]\\
&=\sum_i Q_i m_i\frac{s}{100}.
\end{aligned}
$$

Yani türev, fiyat hareketinin etkisini anlamamızı sağlıyor. Etki sabit eğimle büyüdüğü için doğrudan yeniden değerleme de aynı sonucu veriyor.

## 5. Gerçek ASELS–THYAO replay’iyle hesap

Doğrulanan çoklu hisse koşusunun sonunda ASELS’te **−15**, THYAO’da **+100** pay vardı. Dashboard’da şoku **−1** seçelim.

| Hisse | Net pozisyon $Q_i$ | Referans fiyat $m_i$ | %1 düşüşte fiyat farkı $\Delta m_i$ | Kâr/zarar etkisi $Q_i\Delta m_i$ |
|---|---:|---:|---:|---:|
| ASELS | −15 | 410,125 ₺ | −4,10125 ₺ | +61,51875 ₺ |
| THYAO | +100 | 288,125 ₺ | −2,88125 ₺ | −288,125 ₺ |
| **Toplam** | | | | **−226,60625 ₺** |

ASELS kısa pozisyonda. Fiyat düştüğünde iki eksi çarpıldığı için **+61,51875 ₺** etki veriyor. THYAO uzun pozisyonda. Aynı düşüş **−288,125 ₺** etki veriyor. ASELS’in katkısı THYAO’nun kaybının bir kısmını dengeliyor.

Zincir kuralı üzerinden aynı sonucu buluruz:

$$
\begin{aligned}
\frac{d\Phi}{ds}
&=\frac{(-15)\times410{,}125+100\times288{,}125}{100}
=226{,}60625\ \text{₺/yüzde puanı},\\
\Delta\Pi&=(-1)\times226{,}60625=-226{,}60625\ \text{₺},\\
\Pi_{\text{şok sonrası}}
&=-1\,440{,}375-226{,}60625=-1\,666{,}98125\ \text{₺}.
\end{aligned}
$$

**Okuma biçimi.** Mevcut kâr/zarar **−1.440,375 ₺**. Şokun yarattığı ek etki **−226,60625 ₺**. İkisini topladığımızda senaryo sonucu **−1.666,98125 ₺**. Panel gösterimde basamakları yuvarlayabilir.

## 6. Kodda hangi parça ne yapıyor?

| Adım | Kodun yaptığı iş | Dosya |
|---|---|---|
| Replay’den veri alma | Son geçerli tarihsel orta fiyatı, işaretli pozisyonu ve simüle nakdi okur. Ham fiyatı TL ölçeğine çevirir. | [Simülasyon anlık portföy verisi](dashboard/simulation.py#L838) |
| Portföyü toplama | Seçili bütün hisselerden aynı replay anına ait portföy verisini fiyat şoku hesabına verir. | [Çoklu hisse motoru](dashboard/multi_simulation.py#L167) |
| Kısmi türev ve zincir kuralı | Hisse başına kısmi türevi $Q_i$ olarak verir. İşaretli piyasa değerini $Q_i m_i$ olarak hesaplar, 100’e böler ve girilen şokla çarpar. Hisse etkilerini toplar. | [Stres hesabı](dashboard/portfolio_risk.py#L22) |
| Sonucu gösterme | Toplam kâr/zararı, şok etkisini ve hisse tablosunu Türkçe gösterir. Hesap replay anı değiştikçe güncellenir. | [Dashboard’a aktarma](dashboard/server.py#L530) · [Paneli güncelleme](dashboard/static/app.js#L430) |

Motor hesaplarda Python **Fraction** kullanır. Örneğin 410,125 ₺’lik fiyat ve 1,25 yüzde puanlık şok hesap sırasında kesir olarak tutulur. Sunucudan ekrana sonuç gönderilirken sayısal gösterim için dönüşüm yapılır.

Panel −100 ile +100 arasında ortak yüzde şoku kabul eder. Açık pozisyonu olan bir hissenin geçerli referans fiyatı yoksa portföy toplamı ve şok sonucu eksik gösterilir. Açık pozisyonu sıfır olan bir hissenin fiyat etkisi sıfırdır ve hissenin nakit bakiyesi toplamda yer alır. [Girdi ve eksik fiyat kontrolü](dashboard/portfolio_risk.py#L16)

Testler iki yolu karşılaştırır. İlk yol yukarıdaki türev hesabını kullanır. İkinci yol şokla değişen fiyatları tek tek nakit artı pozisyon değeri formülüne koyar. Test ayrıca bir hissenin şokunu $+h$ ve $-h$ yapıp diğerlerini sabit tutarak **merkezi farkla** türevi denetler. Dashboard entegrasyon testi, şok uygulanırken gerçek replay pozisyonunun ve emir günlüğünün aynı kaldığını kontrol eder. [Matematik testleri](tests/test_portfolio_risk.py#L8) · [Dashboard testi](tests/strategy_dashboard_test.py#L161)

## 7. Hocanın sorabileceği kısa sorular

**Neden kısmi türev?** Portföy kâr/zararı birden fazla hisse fiyatına bağlı. Diğer fiyatları sabit tutup tek hissenin etkisini ölçüyorum.

**Zincir kuralını nerede kullandın?** Girdi TL cinsinden fiyat farkı yerine bir yüzde. Önce yüzde değişiminin her hissenin fiyatına etkisini, sonra o fiyat değişiminin portföy kâr/zararına etkisini alıp çarpıyorum.

**Pozisyon sıfırsa türev ne olur?** Açık pozisyon fiyat değişimine duyarlı olmadığı için türev sıfır. Önceden oluşan kâr/zarar mevcut portföy değerinin parçası olarak kalır.

**Bu bir tahmin mi?** Bu bir stres senaryosu. Verdiğim yüzde hareketi gerçekleşmiş varsayıyorum ve portföyü o fiyatlarla yeniden değerliyorum. Pozisyonlar, nakit ve gerçekleşen işlemler o replay anından alınıyor.

**Türevle hesaplanan etki neden doğrudan hesapla aynı?** Sabit pozisyonla portföy değeri fiyata doğrusal bağlı. Dolayısıyla eğim değişmiyor ve fiyat farkını eğimle çarpmak tam sonucu veriyor.

## 8. Sunumda kullanabileceğin anlatım

> Portföyümüzde aynı anda birden fazla hisse var. Kâr/zarar fonksiyonunda her hisse için nakdi ve eldeki pay adediyle fiyatın çarpımını topladım. Sadece ASELS fiyatını değiştirip diğer fiyatları sabit tutunca, ASELS fiyatına göre kısmi türev net ASELS pozisyonunu veriyor. Panelde kullanıcı yüzde giriyor. Zincir kuralıyla önce bu yüzdenin her hisse fiyatını kaç TL değiştirdiğini, sonra o fiyat değişiminin portföye etkisini hesapladım. Hisse başına etkileri toplayınca şok sonrası kâr/zararı buluyoruz. Örneğin gerçek replay’in sonunda ortak %1 düşüş ASELS kısa pozisyonundan kazanç, THYAO uzun pozisyonundan kayıp üretti ve portföy kâr/zararını toplam 226,61 TL azalttı.
