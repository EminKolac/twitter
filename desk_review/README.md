# Paper short desk — inceleme ve yama seti (26 Ekim 2026)

Kapsam: `STRATEJI_CLAUDE.md` içindeki kodun tamamı (`desklib/*`, `run_desk.py`, `DESK_RULES.md`) ve 9–26 Ekim
paper sonuçları. **Borsa verisine erişim yoktu.** Bu yüzden aşağıdaki sayılar üç kaynaktan geliyor: (1) kodun kendisi
üzerinde birebir cebir, (2) bildirilen 33 TP / 62 STOP / 52 BE_STOP karışımına kalibre edilmiş bir Monte Carlo modeli
(**model, backtest değil**), (3) sentetik mumlarla gerçek kodun çalıştırıldığı tekrar üretim testleri. Her bulgu ayrı bir
ajan tarafından çürütülmeye çalışıldı; aşağıda yalnızca bu denetimden geçenler var.

## 1. Kısa cevap

1. **+0.152R sıfırdan ayırt edilemiyor.** %95 güven aralığı işlemler bağımsızsa ≈ [−0.08, +0.40], aynı saatte açılan
   korelasyonlu işlemler hesaba katılınca ≈ [−0.16, +0.46]. 17 gün, tek bir piyasa rejimi.
2. **Kazanma oranı kapısı aslında FAIL.** `auditor.stats()` `r > 0` sayıyor. BE stop `entry*(1-2·fee-2·slip)` olduğu için
   her BE_STOP +0.0001…+0.0004R yazılıyor ve 52 BE_STOP'un ~43'ü “kazanç” sayılıyor (76/147 = %51.7). Gerçek kazanç
   (≥ +0.10R) = yalnızca TP = 33/147 = **%22.4** [Wilson %16.5–29.8]. Eşik değişmiyor; ölçüm düzeltiliyor.
3. **Kayıt büyük olasılıkla iyimser.** Üç kod hatası paper sonucunu yukarı itiyor: kesinti sonrası catch-up'ta TP/zone
   bilgisinin geleceği görmesi, BE stop'un mumun açılışının üstünde kaldığı halde BE fiyatından dolması ve aynı mumda
   stop olan bazın üstüne add açılması (aşağıda §3). Önce bunlar temizlenmeli, sonra parametre tartışılmalı.
4. **Matematik ne diyor:** E = p1·A − (1 − p1). Burada p1 = +1R'ye −1R'den önce ulaşma oranı = 85/147 = 0.578, A = +1R'ye
   ulaşan işlemin ortalama sonucu = 0.388 × 2.556R = 0.992R. Mevcut A ile +0.4R için **p1 ≥ 0.70** gerekiyor (bugün 0.58).
   Yani boşluk büyük ölçüde **giriş kalitesi** sorunu.
5. **Tek açık soru BE kuralı.** Veriye en iyi uyan model (fiyat +1R'den sonra da hafifçe lehte akıyor) BE'yi kaldırmanın
   +0.17R kazandırdığını söylüyor. Neredeyse aynı derecede uyan diğer model (+1R'den sonra yazı-tura) ise ±0.02R diyor.
   Bunu tahminle değil **sizin 52 BE_STOP işleminizle** çözün: `research/replay_be.py` (aşağıda §5).
6. **Yapılmaması gerekenler:** +1R'de kısmi kâr alma, ATR trailing, time stop, TP'yi öne çekme. Uyan modellerin hiçbirinde
   expectancy'yi artırmıyorlar, çoğunda düşürüyorlar (§6).

## 2. Sonucun ayrıştırması (bildirilen karışımdan, birebir)

| büyüklük | değer | not |
|---|---|---|
| STOP net R | −1.0000 | maliyetler risk tutarına dahil; her mesafede tam −1R |
| BE_STOP net R | +0.00002 … +0.0004 | 4 hane yuvarlamada çoğu +0.0001…+0.0004 → eski auditor'da “kazanç” |
| ortalama TP | **+2.556R** net (≈3.0 fiyat-R) | (0.152·147 + 62) / 33 |
| p1 = P(+1R önce) | **0.578** [0.50–0.66] | rastgele yürüyüşte 0.50 → girişte bir avantaj var |
| q = P(TP \| +1R) | **0.388** [0.29–0.50] | |
| +0.4R için gereken | p1 ≥ 0.703 **veya** A ≥ 1.42 | A ≥ 1.42 ⇔ q ≥ 0.556 ya da ort. TP ≥ 3.66R |
| maliyet / R | ort. ≈ %13; 0.3% stopta %32–40 | MIN_STOP_PCT 0.25% çok düşük |

Fiyat yolu modelleri (kalibrasyon): rastgele yürüyüş **reddedildi**. +1R sonrası geri dönüş (mean reversion) de
**reddedildi**. Sabit lehte sürüklenme (b) iyi uyuyor (p≈0.81). “Avantaj +1R'de bitiyor” modeli (c) sınırda (p≈0.06–0.17,
kümelenmeye göre). İkisinin olabilirlik oranı ≈ 2.5–4.6:1, yani **karar verilemez**.

## 3. Kod hataları — yama setinde düzeltildi (varsayılan AÇIK)

Hepsi sentetik mumlarla gerçek kod üzerinde tekrar üretildi. Etki sütunu yönü veriyor, büyüklük sizin logunuza bağlı.

| # | sorun | dosya | etki | düzeltme |
|---|---|---|---|---|
| 1 | **Catch-up lookahead.** Kesintiden sonra watchlist/zone'lar *şimdiki* veriyle kurulup 48 saate kadar geçmiş saatler yeniden oynatılıyor. TP, sinyalden *sonra* basılmış dibi görüyor; sonradan invalid olan (genelde kaybeden) zone'lar hiç işlem üretmiyor; flip daha oluşmadan işlem açılabiliyor. | `run_desk.py`, `cartographer.py` | sonucu **yukarı** iter | yalnızca en yeni kapanmış 1H mum işlem görebilir (`TRADE_LATEST_HOUR_ONLY`); eski saatler onaylanır ama “late” diye reddedilir |
| 2 | **Çalışma sırası.** Her 1H kapanışta risk_officer, paper.manage'den *önce* çalışıyor. Aynı mumda BE stop'u vurulan baz hâlâ açık görünüyor, sinyal “add” olarak onaylanıyor ve baz ardından kapanıyor (sahibi olmayan add). Aynı mumda boşalan slot da görülmüyor. | `run_desk.py` | “stop olan add'ler”in bir kısmı | saat saat olay sırası: manage → confirm → risk → gate → open |
| 3 | **Gap-through BE dolumu.** BE fitil ile kuruluyor ama mum BE'nin üstünde kapanmış olabiliyor. Sonraki mum stopun üstünde açılıyor, yine de BE fiyatından (~0R) dolduruluyor. Gerçekte açılışta (−0.1…−0.5R) dolar. | `paper.py` | BE_STOP'ları ve WR'yi **yukarı** iter | `GAP_AWARE_FILLS`: dolum = max(stop, açılış) |
| 4 | **Zone tekrar işlem görüyor.** `confirmed` bayrağı yalnızca bir önceki zones.csv'den taşınıyor. Coin watchlist'ten çıkıp geri girince aynı zone (genelde zaten stop olmuş olan) yeniden işlem açıyor. Kural “her zone bir kez” diyor. | `cartographer.py` | küçük, olumsuz | `signals.csv`'de görülen zone_id bir daha onaylanmaz |
| 5 | **Watchlist dışı coin'e add/giriş.** Açık pozisyon için tutulan zone'lar sinyal üretiyor. Başlangıç noktası 10 gün geri çekildiği için TP flip öncesi diplere kayıyor, RR şişiyor ve add onaylanıyor. | `cartographer.py`, `risk_officer.py` | add'ler aleyhine | yalnızca güncel watchlist'teki coin'ler sinyal üretir ve add alır |
| 6 | **MAX_ADDS sızıntısı.** Yalnızca *açık* bacakları sayıyor; bacaklar BE'de kapandıkça aynı flip'e sınırsız add açılabiliyor, add_number tekrar ediyor. | `risk_officer.py` | add aleyhine | tüm açık bacaklar + aynı flip'in (anchor ±%0.5) kapanmış bacakları sayılır; benzersiz add_number |
| 7 | **Kaçırılan / geciken EXIT NOW.** Kesinti ya da günlük veri hatasında anchor üstü kapanış kaçabiliyor. Sonraki 1H mumlarda TP gelirse kural yerine TP yazılıyor (iyimser). | `paper.py`, `exit_clerk.py` | nadir, iyimser | EXIT NOW 1H yürüyüşünde olay sırasıyla uygulanır (00:00 UTC'de biten 1H mum = günlük kapanış). exit_clerk yedek kontrol olarak kalır |
| 8 | **WR ölçümü** (yukarıdaki §1.2). | `auditor.py` | kapı PASS → **FAIL** | kazanç = R ≥ +0.10 (eskisinin alt kümesi, yalnızca sıkılaştırır); eski oran raporda bağlam olarak kalır |
| 9 | **DD yalnızca realize.** Açık korelasyonlu pozisyonların geri verdiği kâr görünmüyor. | `auditor.py`, `run_desk.py` | DD kapısı **iyimser** | her çalıştırmada MTM equity kaydı (`equity.csv`); kapı realize ve MTM'in kötüsü |
| 10 | **Korelasyonlu girişler.** Tek 1H mumda 5 coin × %1 = %5 risk açılabiliyor (tekrar üretildi). Kümelenmiş modelde P(DD > %20) %5 → %19 (3'lü) → %37 (5'li). 15.9% PASS kırılgan. | `risk_officer.py` | DD | mum başına en fazla 2 yeni coin; BE'ye gelmemiş açık risk ≤ %3 |
| 11 | **Ölçüm eksikliği.** Karar anındaki özellikler kaydedilmiyor; filtreler test edilemiyor. | `risk_officer.py`, `paper.py` | — | plan başına `kind, rr_net, cost_share, stop_atr, cluster_n, flip_age_d, btc_x, late_h`; pozisyonda `mfe_r, mae_r, be_ms` |

Ayrıca: `confirm()` içindeki yorum “1H close above zone top → zone broken” diyordu ama kod bunu yapmıyordu. Kural
metni (yalnızca 4H kapanışla invalid) ile kod tutarlıydı. Yorum kaldırıldı, davranış test edilebilir bir bayrak oldu.

## 4. Hipotezler — kodda bayrak olarak var, varsayılan KAPALI (sırayla test edin)

| sıra | değişiklik | bayrak | neden | test / karar kuralı (önceden kayıtlı) | model etkisi |
|---|---|---|---|---|---|
| 1 | **BE kuralı**: kapanışla kur / net +1R / daha geç / hiç | `BE_MODE`, `BE_TRIGGER_UNITS`, `BE_TRIGGER_R` | En büyük çıkış kaldıracı; işareti bilinmiyor | `replay_be.py`: 52 BE_STOP orijinal stopla. Ort. R ≥ +0.25 → devam var, kapanış-tabanlı/geç BE'yi forward test et. ≤ 0 → BE kalsın, girişlere odaklan | (b) no-BE +0.17, close +0.04, +2R +0.11; (c) hepsi ≈ 0 ve no-BE'de DD P95 %27 → %36 |
| 2 | **Maliyet payı tabanı** | `MAX_COST_SHARE` (0.15–0.30) | 0.3% stopta maliyet 1R'nin %32–40'ı; BE stop tetikleyicinin dibinde | `analyze_log.py` stop/maliyet kovaları: dar kovalarda ort. R < genel ve BE payı yüksekse, logun **iki yarısında da** iyileşiyorsa aç | ≤%15: +0.04R (b), +0.03R (c); işlemlerin ~%32'si düşer |
| 3 | **Add'ler** | `MAX_ADDS` (0) | Add'ler bazla aynı geri çekilmede kapanıyor; R'leri tam işlem gibi sayılıyor | add'lerin ort. R < baz işlemlerin ort. R ise `MAX_ADDS = 0` (karşı-olgu birebir: baz-only ort. R) | veriye bağlı |
| 4 | **Net RR** | `MIN_RR_NET` (1.0) | Brüt RR 1.0 gate'i net +0.11…+0.87R TP'li işlemleri alıyor | RR_net < 1 kovasının ort. R'si genelin altındaysa aç | küçük |
| 5 | **BTC impuls filtresi** | `BTC_IMPULSE_ATR` (≈1.5) | Aynı saatteki alt REJECT'leri çoğunlukla BTC squeeze'i | `btc_x` terciller: üst tercil ≥0.2R kötüyse ve her kovada ≥30 işlem varsa aç | bilinmiyor (DD için olumlu) |
| 6 | **Onay kalitesi** | `REJECT_MAX_CLOSE_LOC` 0.4, `INSIDE_MAX_DEPTH` 0.5, `MIN_PEN_FRAC` 0.25 | Yeşil itiş mumları REJECT sayılıyor; zone tepesine yakın INSIDE kapanışları | sinyal mumlarından (signals.csv OHLC) kova testi | bilinmiyor |
| 7 | **1H kapanışla zone kırılması** | `ZONE_BREAK_ON_1H_CLOSE` | Saatler önce basılmış tepenin altına stop | işaretli işlemlerin ort. R'si ≥0.3R kötüyse ve n≥20 ise aç | bilinmiyor |
| 8 | **Flip yaşı** | `MAX_FLIP_AGE_DAYS` | 19 güne kadar eski flip'ler işlem görüyor | `flip_age_d` kovaları | bilinmiyor |

## 5. Hemen çalıştırın (borsa erişimi olan makinede, salt-okunur)

```bash
python3 research/analyze_log.py /workspace/desk     # sadece CSV'ler: dürüst skor kartı, kovalar, hatadan etkilenen işlemler
python3 research/replay_be.py   /workspace/desk     # public 1H/1D mumlar: BE kararı + gap-through dolumların gerçek etkisi
```

`analyze_log.py` §3'teki hatalardan etkilenen işlemleri tek tek sayar (late, stale add, tekrar zone, add sızıntısı,
watchlist dışı) ve **temizlenmiş** örneklemin skorunu verir. Asıl başlangıç noktanız bu temiz rakam.

## 6. Önermediklerim (model + cebir)

| değişiklik | (b) sürüklenme modeli | (c) yazı-tura modeli | yorum |
|---|---|---|---|
| +1R'de 1/3 kısmi | +0.10 (−0.05) | ≈ 0 | gerçek WR'yi ~%57'ye çıkarır, expectancy'yi düşürür. WR kapısı için cazip ama +0.4R'den uzaklaştırır |
| +1R'de 1/2 kısmi | +0.07 (−0.08) | ≈ 0 | aynı |
| ATR trailing 1.5× / 3× | −0.09 / −0.03 | ≈ +0.01 | TP zaten hareketi sınırlıyor |
| time stop 24h / 48h | −0.07 / −0.03 | ≈ 0 | |
| TP'yi öne çekme (0.1R / %5) | ≈ −0.01 | ≈ 0 | bedava kaldıraç değil |

## 7. Süreç ve istatistik

- 0.15R ile 0.40R'yi ayırmak için ~**220 bağımsız işlem** gerekiyor (%80 güç, tek yönlü). Kümelenmeyle 290–440 işlem
  ediyor: yeni kurallarla ≈5–7 hafta ya da mevcut örneklemle birleştirerek ≈2.5–5 hafta.
- Aynı 147 işlemde 10 varyant deneyip en iyisini seçmek tek başına **+0.06R** (çıkış varyantları, eşli) ile
  **+0.06…+0.18R** (giriş filtreleri, işlemlerin %10–50'sini atan) arası sahte iyileşme üretir. Kuralı önceden yazın,
  logun iki yarısında doğrulayın, sonra **tek değişiklikle** forward paper yapın.
- Çıkış kuralları aynı girişler üzerinde **eşli** karşılaştırılabildiği için çok daha az işlemle karar verilebilir.
  `replay_be.py` bu yüzden ilk adım.

## 8. Dosyalar

- `desk/` — yamalı kod (doğruluk düzeltmeleri açık, hipotezler kapalı) ve güncel `DESK_RULES.md`.
  `desk/desklib/charts.py` bir **taslak (stub)**: orijinal pakette yoktu, kendi dosyanızın üzerine kopyalamayın.
  Eski `data/` klasörüyle çalışır: yeni sütunlar boş başlar, açık pozisyonların MFE/MAE'si geçmiş mumlardan doldurulur.
- `desk.patch` — orijinale göre birleşik diff
- `research/analyze_log.py`, `research/replay_be.py` — salt-okunur analiz araçları
- Eşikler (`THRESHOLDS`) değişmedi. Win rate ve DD **tanımları** yalnızca sıkılaştı. Kilitli metrik tanımı olduğu için bu
  iki değişikliği yine de siz onaylayın.
