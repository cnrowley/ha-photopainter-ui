package main

import (
    "crypto/rand"
    "encoding/binary"
    "encoding/json"
    "fmt"
    "io"
    "os"
    "path/filepath"
    "strconv"
    "time"
)

// ============================================================
// Configuration
// ============================================================

const (
    W         = 800
    H         = 480
    TotalPix  = int64(W) * int64(H)
    WalkersPF = 1200
    MaxSteps  = 6000
    ThickRad  = 1
)

// ============================================================
// Color + BMP
// ============================================================

type Color uint8

const (
    WHITE Color = iota
    BLACK
    BLUE
    GREEN
    RED
    YELLOW
)

type RGB struct {
    r, g, b uint8
}

func toRGB(c Color) RGB {
    switch c {
    case BLACK:
        return RGB{0, 0, 0}
    case BLUE:
        return RGB{0, 0, 255}
    case GREEN:
        return RGB{0, 200, 0}
    case RED:
        return RGB{200, 0, 0}
    case YELLOW:
        return RGB{200, 200, 0}
    default:
        return RGB{255, 255, 255}
    }
}

func writeBMP24(path string, pix []Color) error {
    rowBytes := (3*W + 3) &^ 3
    dataSize := rowBytes * H
    fileSize := 54 + dataSize

    f, err := os.Create(path)
    if err != nil {
        return err
    }
    defer f.Close()

    header := make([]byte, 54)
    header[0] = 'B'
    header[1] = 'M'
    binary.LittleEndian.PutUint32(header[2:], uint32(fileSize))
    binary.LittleEndian.PutUint32(header[10:], 54)
    binary.LittleEndian.PutUint32(header[14:], 40)
    binary.LittleEndian.PutUint32(header[18:], uint32(W))
    binary.LittleEndian.PutUint32(header[22:], uint32(H))
    binary.LittleEndian.PutUint16(header[26:], 1)
    binary.LittleEndian.PutUint16(header[28:], 24)
    binary.LittleEndian.PutUint32(header[34:], uint32(dataSize))

    if _, err := f.Write(header); err != nil {
        return err
    }

    row := make([]byte, rowBytes)
    for y := H - 1; y >= 0; y-- {
        i := 0
        for x := 0; x < W; x++ {
            c := toRGB(pix[y*W+x])
            row[i] = c.b
            row[i+1] = c.g
            row[i+2] = c.r
            i += 3
        }
        for ; i < rowBytes; i++ {
            row[i] = 0
        }
        if _, err := f.Write(row); err != nil {
            return err
        }
    }

    return nil
}

// ============================================================
// Helpers
// ============================================================

func idxOf(x, y int) int {
    return y*W + x
}

func wrap(x, m int) int {
    if x < 0 {
        return x + m
    }
    if x >= m {
        return x - m
    }
    return x
}

// ============================================================
// Serializable RNG (xorshift64*)
// ============================================================

type RNG struct {
    State uint64
}

func newRNG(seed uint64) RNG {
    if seed == 0 {
        seed = 0x9e3779b97f4a7c15
    }
    return RNG{State: seed}
}

func (r *RNG) Uint64() uint64 {
    x := r.State
    x ^= x >> 12
    x ^= x << 25
    x ^= x >> 27
    r.State = x
    return x * 2685821657736338717
}

func (r *RNG) Intn(n int) int {
    if n <= 0 {
        panic("Intn with n <= 0")
    }
    return int(r.Uint64() % uint64(n))
}

// uniform int in [lo, hi]
func (r *RNG) RangeInt(lo, hi int) int {
    return lo + r.Intn(hi-lo+1)
}

// jitter in [-0.25, 0.25)
func (r *RNG) JitterQuarter() float64 {
    const denom = float64(1 << 53)
    v := float64(r.Uint64()>>11) / denom // [0,1)
    return -0.25 + 0.5*v
}

// ============================================================
// Layer
// ============================================================

type Layer struct {
    Occ   []byte
    Color Color
    RNG   RNG
}

func newLayer() Layer {
    return Layer{
        Occ:   make([]byte, W*H),
        Color: WHITE,
        RNG:   newRNG(1),
    }
}

// ============================================================
// Checkpoint I/O
// ============================================================

type checkpointJSON struct {
    Frame int `json:"frame"`
}

func saveCheckpoint(dir string, frame int, layers []Layer) error {
    jpath := filepath.Join(dir, "checkpoint.json")
    bpath := filepath.Join(dir, "checkpoint.bin")

    jf, err := os.Create(jpath)
    if err != nil {
        return err
    }
    enc := json.NewEncoder(jf)
    enc.SetIndent("", "  ")
    if err := enc.Encode(checkpointJSON{Frame: frame}); err != nil {
        _ = jf.Close()
        return err
    }
    if err := jf.Close(); err != nil {
        return err
    }

    bf, err := os.Create(bpath)
    if err != nil {
        return err
    }
    defer bf.Close()

    for _, L := range layers {
        if len(L.Occ) != W*H {
            return fmt.Errorf("invalid layer occupancy length")
        }
        if _, err := bf.Write(L.Occ); err != nil {
            return err
        }
        if err := binary.Write(bf, binary.LittleEndian, L.RNG.State); err != nil {
            return err
        }
    }

    return nil
}

func loadCheckpoint(dir string, frame *int, layers []Layer) (bool, error) {
    jpath := filepath.Join(dir, "checkpoint.json")
    bpath := filepath.Join(dir, "checkpoint.bin")

    jf, err := os.Open(jpath)
    if err != nil {
        if os.IsNotExist(err) {
            return false, nil
        }
        return false, err
    }
    defer jf.Close()

    bf, err := os.Open(bpath)
    if err != nil {
        if os.IsNotExist(err) {
            return false, nil
        }
        return false, err
    }
    defer bf.Close()

    var cj checkpointJSON
    if err := json.NewDecoder(jf).Decode(&cj); err != nil {
        return false, err
    }
    *frame = cj.Frame

    for i := range layers {
        if _, err := io.ReadFull(bf, layers[i].Occ); err != nil {
            return false, err
        }
        if err := binary.Read(bf, binary.LittleEndian, &layers[i].RNG.State); err != nil {
            return false, err
        }
    }

    return true, nil
}

// ============================================================
// Thickening (post-process, Euclidean)
// ============================================================

func thicken(src []byte, out []byte) {
    for i := range out {
        out[i] = 0
    }

    for y := 0; y < H; y++ {
        for x := 0; x < W; x++ {
            if src[idxOf(x, y)] != 0 {
                for dy := -ThickRad; dy <= ThickRad; dy++ {
                    for dx := -ThickRad; dx <= ThickRad; dx++ {
                        if dx*dx+dy*dy <= ThickRad*ThickRad {
                            out[idxOf(wrap(x+dx, W), wrap(y+dy, H))] = 1
                        }
                    }
                }
            }
        }
    }
}

// ============================================================
// Seed helper
// ============================================================

func randomSeed64() uint64 {
    var b [8]byte
    if _, err := rand.Read(b[:]); err == nil {
        return binary.LittleEndian.Uint64(b[:])
    }
    return uint64(time.Now().UnixNano())
}

// ============================================================
// Main
// ============================================================

func main() {
    if len(os.Args) < 2 {
        fmt.Fprintln(os.Stderr, "Usage:\n  ./dla out --init\n  ./dla out --to N")
        os.Exit(1)
    }

    outDir := os.Args[1]
    if err := os.MkdirAll(outDir, 0o755); err != nil {
        fmt.Fprintln(os.Stderr, "Failed to create output directory:", err)
        os.Exit(1)
    }

    initOnly := false
    targetFrame := 100

    if len(os.Args) >= 3 {
        a := os.Args[2]
        if a == "--init" {
            initOnly = true
        } else if a == "--to" && len(os.Args) >= 4 {
            n, err := strconv.Atoi(os.Args[3])
            if err != nil {
                fmt.Fprintln(os.Stderr, "Bad frame number")
                os.Exit(1)
            }
            targetFrame = n
        } else {
            fmt.Fprintln(os.Stderr, "Bad args")
            os.Exit(1)
        }
    }

    layers := make([]Layer, 5)
    for i := range layers {
        layers[i] = newLayer()
    }
    pal := [5]Color{BLUE, GREEN, RED, YELLOW, BLACK}

    curFrame := 0

    if !initOnly {
        ok, err := loadCheckpoint(outDir, &curFrame, layers)
        if err != nil {
            fmt.Fprintln(os.Stderr, "Failed to load checkpoint:", err)
            os.Exit(1)
        }
        if ok {
            fmt.Printf("Resuming from frame %d\n", curFrame)
            for i := 0; i < 5; i++ {
                layers[i].Color = pal[i]
            }
        } else {
            initializeLayers(layers, pal)
            fmt.Println("Initializing frame 0")
            if err := saveCheckpoint(outDir, 0, layers); err != nil {
                fmt.Fprintln(os.Stderr, "Failed to save checkpoint:", err)
                os.Exit(1)
            }
        }
    } else {
        initializeLayers(layers, pal)
        fmt.Println("Initializing frame 0")
        if err := saveCheckpoint(outDir, 0, layers); err != nil {
            fmt.Fprintln(os.Stderr, "Failed to save checkpoint:", err)
            os.Exit(1)
        }
        return
    }

    for f := curFrame + 1; f <= targetFrame; f++ {
        for li := range layers {
            L := &layers[li]
            for p := 0; p < WalkersPF; p++ {
                x := L.RNG.Intn(W)
                y := L.RNG.Intn(H)

                stuck := false
                for s := 0; s < MaxSteps; s++ {
                    var dx, dy int
                    for {
                        dx = L.RNG.RangeInt(-1, 1)
                        dy = L.RNG.RangeInt(-1, 1)
                        if dx != 0 || dy != 0 {
                            break
                        }
                    }

                    x = wrap(x+dx, W)
                    y = wrap(y+dy, H)

                    for ny := -1; ny <= 1 && !stuck; ny++ {
                        for nx := -1; nx <= 1; nx++ {
                            if L.Occ[idxOf(wrap(x+nx, W), wrap(y+ny, H))] != 0 {
                                L.Occ[idxOf(x, y)] = 1
                                stuck = true
                                break
                            }
                        }
                    }

                    if stuck {
                        break
                    }
                }
            }
        }

        if err := saveCheckpoint(outDir, f, layers); err != nil {
            fmt.Fprintln(os.Stderr, "Failed to save checkpoint:", err)
            os.Exit(1)
        }
    }

    img := make([]Color, W*H)
    for i := range img {
        img[i] = WHITE
    }
    thick := make([]byte, W*H)

    for i := 0; i < 5; i++ {
        thicken(layers[i].Occ, thick)
        for p := 0; p < W*H; p++ {
            if thick[p] != 0 {
                img[p] = layers[i].Color
            }
        }
    }

    bmpPath := filepath.Join(outDir, "latest_display.bmp")
    if err := writeBMP24(bmpPath, img); err != nil {
        fmt.Fprintln(os.Stderr, "Failed to write BMP:", err)
        os.Exit(1)
    }

    fmt.Printf("Done. Frame %d saved.\n", targetFrame)
}

func initializeLayers(layers []Layer, pal [5]Color) {
    seed := randomSeed64()
    base := newRNG(seed)

    cols := 3
    rows := 2
    idx := 0

    for r := 0; r < rows && idx < 5; r++ {
        for c := 0; c < cols && idx < 5; c++ {
            x := int((float64(c)+0.5+base.JitterQuarter()) * float64(W) / float64(cols))
            y := int((float64(r)+0.5+base.JitterQuarter()) * float64(H) / float64(rows))

            // Clamp just in case floating-point jitter lands on an edge
            if x < 0 {
                x = 0
            }
            if x >= W {
                x = W - 1
            }
            if y < 0 {
                y = 0
            }
            if y >= H {
                y = H - 1
            }

            layers[idx].Color = pal[idx]
            layers[idx].Occ[idxOf(x, y)] = 1
            layers[idx].RNG = newRNG(seed + uint64(idx))
            idx++
        }
    }
}

