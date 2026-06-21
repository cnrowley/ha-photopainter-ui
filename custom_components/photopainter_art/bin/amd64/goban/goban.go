package main

import (
    "flag"
    "image"
    "image/color"
    "os"
    "strings"

    "golang.org/x/image/bmp"
)

const boardSize = 19
const imgW = 800
const imgH = 480

var palette = map[string]color.RGBA{
    "white":  {255, 255, 255, 255},
    "black":  {0, 0, 0, 255},
    "red":    {255, 0, 0, 255},
    "yellow": {255, 255, 0, 255},
    "blue":   {0, 0, 255, 255},
    "green":  {0, 255, 0, 255},
}

type Stone int

const (
    Empty Stone = iota
    Black
    White
)

type Move struct {
    Color Stone
    X, Y  int
}

type Board struct {
    Grid           [boardSize][boardSize]Stone
    CapturedBlack  int // black stones captured (shown on right)
    CapturedWhite  int // white stones captured (shown on left)
}

////////////////////////////////////////////////////////////////////////////////

func main() {

    input := flag.String("input", "", "SGF file")
    moveNum := flag.Int("move", 0, "Move number")
    output := flag.String("output", "frame.bmp", "Output")

    bgColor := flag.String("bg", "white", "white|black")
    boardColor := flag.String("board", "yellow", "yellow|white")
    whiteColor := flag.String("white-color", "green", "white|green|blue|red")
    blackColor := flag.String("black-color", "black", "black|red")

    gridThickness := flag.Int("grid-thickness", 1, "1 or 2")
    highlightMode := flag.String("highlight", "ring", "dot|ring|none")

    flag.Parse()

    data, _ := os.ReadFile(*input)
    moves := parseSGF(string(data))

    board := Board{}
    for i := 0; i < *moveNum && i < len(moves); i++ {
        board.Play(moves[i])
    }

    img := render(board, moves, *moveNum,
        *gridThickness,
        *bgColor, *boardColor,
        *whiteColor, *blackColor,
        *highlightMode)

    f, _ := os.Create(*output)
    defer f.Close()
    bmp.Encode(f, img)
}

////////////////////////////////////////////////////////////////////////////////
// SGF

func parseSGF(s string) []Move {
    var moves []Move
    s = strings.ReplaceAll(s, "\n", "")
    for _, t := range strings.Split(s, ";") {
        if strings.HasPrefix(t, "B[") || strings.HasPrefix(t, "W[") {
            col := Black
            if t[0] == 'W' {
                col = White
            }
            x := int(t[2] - 'a')
            y := int(t[3] - 'a')
            moves = append(moves, Move{col, x, y})
        }
    }
    return moves
}

////////////////////////////////////////////////////////////////////////////////
// RULES (with capture tracking)

func (b *Board) Play(m Move) {
    if !inBounds(m.X, m.Y) {
        return
    }

    b.Grid[m.Y][m.X] = m.Color

    enemy := Black
    if m.Color == Black {
        enemy = White
    }

    for _, d := range [][2]int{{1, 0}, {-1, 0}, {0, 1}, {0, -1}} {
        nx, ny := m.X+d[0], m.Y+d[1]
        if inBounds(nx, ny) && b.Grid[ny][nx] == enemy {
            if !b.hasLiberty(nx, ny, make(map[[2]int]bool)) {
                b.removeGroup(nx, ny)
            }
        }
    }
}

func (b *Board) hasLiberty(x, y int, visited map[[2]int]bool) bool {
    key := [2]int{x, y}
    if visited[key] {
        return false
    }
    visited[key] = true

    color := b.Grid[y][x]

    for _, d := range [][2]int{{1, 0}, {-1, 0}, {0, 1}, {0, -1}} {
        nx, ny := x+d[0], y+d[1]
        if !inBounds(nx, ny) {
            continue
        }
        if b.Grid[ny][nx] == Empty {
            return true
        }
        if b.Grid[ny][nx] == color {
            if b.hasLiberty(nx, ny, visited) {
                return true
            }
        }
    }
    return false
}

func (b *Board) removeGroup(x, y int) {
    color := b.Grid[y][x]
    stack := [][2]int{{x, y}}
    count := 0

    for len(stack) > 0 {
        p := stack[len(stack)-1]
        stack = stack[:len(stack)-1]

        px, py := p[0], p[1]
        if !inBounds(px, py) || b.Grid[py][px] != color {
            continue
        }

        b.Grid[py][px] = Empty
        count++

        for _, d := range [][2]int{{1, 0}, {-1, 0}, {0, 1}, {0, -1}} {
            stack = append(stack, [2]int{px + d[0], py + d[1]})
        }
    }

    if color == Black {
        b.CapturedWhite += count
    } else {
        b.CapturedBlack += count
    }
}

func inBounds(x, y int) bool {
    return x >= 0 && y >= 0 && x < boardSize && y < boardSize
}

////////////////////////////////////////////////////////////////////////////////
// RENDER

func render(board Board, moves []Move, moveNum int,
    gridThickness int,
    bgName, boardName, whiteName, blackName string,
    highlightMode string) *image.RGBA {

    img := image.NewRGBA(image.Rect(0, 0, imgW, imgH))

    bg := palette[bgName]
    boardCol := palette[boardName]
    whiteCol := palette[whiteName]
    blackCol := palette[blackName]

    fill(img, bg)

    margin := 20
    targetBoardPx := imgH - 2*margin
    cell := targetBoardPx / (boardSize - 1)
    boardPx := cell * (boardSize - 1)

    offsetX := (imgW-boardPx)/2
    offsetY := margin

    fillRect(img, offsetX, offsetY, boardPx, boardPx, boardCol)

    gridCol := palette["black"]

    for i := 0; i < boardSize; i++ {
        x := offsetX + i*cell
        y := offsetY + i*cell

        drawLine(img, x, offsetY, x, offsetY+boardPx, gridCol, gridThickness)
        drawLine(img, offsetX, y, offsetX+boardPx, y, gridCol, gridThickness)
    }

    r := cell/2 - 3

    // stones
    for y := 0; y < boardSize; y++ {
        for x := 0; x < boardSize; x++ {
            cx := offsetX + x*cell
            cy := offsetY + y*cell

            switch board.Grid[y][x] {
            case Black:
                circle(img, cx, cy, r, blackCol)
            case White:
                circle(img, cx, cy, r, whiteCol)
                circleOutline(img, cx, cy, r, palette["black"])
            }
        }
    }

    // highlight
    if highlightMode != "none" && moveNum > 0 && moveNum <= len(moves) {
        m := moves[moveNum-1]
        cx := offsetX + m.X*cell
        cy := offsetY + m.Y*cell

        if highlightMode == "dot" {
            circle(img, cx, cy, cell/6, palette["red"])
        } else {
            circleOutline(img, cx, cy, cell/2-1, palette["red"])
        }
    }

    // ✅ DRAW CAPTURES (NEW)
    drawCaptureGrids(img, board, offsetX, offsetY, boardPx, r)

    return img
}

////////////////////////////////////////////////////////////////////////////////
// CAPTURE GRIDS

func drawCaptureGrids(img *image.RGBA, b Board, offsetX, offsetY, boardPx, r int) {

    spacing := r*2 + 4

    leftX := offsetX - spacing
    rightX := offsetX + boardPx + spacing

    topY := offsetY

    perCol := (boardPx / spacing)

    // LEFT = captured white (display as black stones)
    for i := 0; i < b.CapturedWhite; i++ {
        x := leftX - (i/perCol)*spacing
        y := topY + (i%perCol)*spacing
        circle(img, x, y, r/2, palette["black"])
    }

    // RIGHT = captured black (display as white/green/etc stones)
    for i := 0; i < b.CapturedBlack; i++ {
        x := rightX + (i/perCol)*spacing
        y := topY + (i%perCol)*spacing
        circle(img, x, y, r/2, palette["white"])
        circleOutline(img, x, y, r/2, palette["black"])
    }
}

////////////////////////////////////////////////////////////////////////////////
// DRAW

func fill(img *image.RGBA, c color.RGBA) {
    for y := 0; y < imgH; y++ {
        for x := 0; x < imgW; x++ {
            img.Set(x, y, c)
        }
    }
}

func fillRect(img *image.RGBA, x, y, w, h int, c color.RGBA) {
    for yy := y; yy < y+h; yy++ {
        for xx := x; xx < x+w; xx++ {
            img.Set(xx, yy, c)
        }
    }
}

func drawLine(img *image.RGBA, x0, y0, x1, y1 int, c color.RGBA, t int) {
    for d := -t/2; d <= t/2; d++ {
        if x0 == x1 {
            for y := y0; y <= y1; y++ {
                img.Set(x0+d, y, c)
            }
        } else {
            for x := x0; x <= x1; x++ {
                img.Set(x, y0+d, c)
            }
        }
    }
}

func circle(img *image.RGBA, cx, cy, r int, col color.RGBA) {
    for dy := -r; dy <= r; dy++ {
        for dx := -r; dx <= r; dx++ {
            if dx*dx+dy*dy <= r*r {
                img.Set(cx+dx, cy+dy, col)
            }
        }
    }
}

func circleOutline(img *image.RGBA, cx, cy, r int, col color.RGBA) {
    for dy := -r; dy <= r; dy++ {
        for dx := -r; dx <= r; dx++ {
            d2 := dx*dx + dy*dy
            if d2 <= r*r && d2 >= (r-1)*(r-1) {
                img.Set(cx+dx, cy+dy, col)
            }
        }
    }
}

